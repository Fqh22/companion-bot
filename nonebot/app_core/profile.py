"""自我画像：把「我是谁、他在干什么」从一段自由文本变成固定字段。

**为什么要改**：原来 `learn.review()` 每天生成一段 260 字的自由文本存进
`self_profile.md`，但它**从来没进过对话 prompt**——只有 `kb.py` 把它当知识
文件索引，以及 review 自己读回去。等于每天认真写一份自我介绍，然后不用。

自由文本还有两个毛病：
1. 无法比较——今天写的和昨天写的没法 diff，也就没法知道它到底有没有变准；
2. 无法降级——整段 260 字要么全注入要么全不注入，错一条就污染一整段。

改成固定字段后，每条能单独带置信度和来源，某一条不可信就单独不注入。

**这一版只做写入，不做注入**：`profile.json` 先攒几天，确认字段稳定、
模型真的会填，再接 prompt。接的时候关掉开关就能退回现状。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

# 固定字段与顺序。顺序就是注入时的顺序——稳定的放在前面，
# 变化快的（正在做的事）放后面。
FIELDS = ("职业与求职", "家庭", "技术与项目", "审美与偏好", "作息与身体", "明确讨厌")

# 每个字段一句话，太长就截。注入时总共控制在 200 字内。
FIELD_MAX = 46
COMPACT_MAX = 200
# 低于这个置信度的字段不注入：画像是每轮常驻的，错一条会一直错。
MIN_CONF = 0.6
# 画像会过期：这么久没有**新证据**，就不信了，也不再注入、并从库里删掉。
# 为什么必须有这个：他会变。一个半年前的「他在找工作」，会一直把新对话往旧解释。
# 不搞渐进衰减（对折对折会让阈值附近变得难以解释）：要么有证据，要么到期作废。
DROP_AFTER_DAYS = 120


def _value(v) -> tuple[str, float]:
    """一个字段的 (文本, 置信度)。兼容「纯字符串」和 {"text","confidence"} 两种写法。"""
    if isinstance(v, dict):
        text = v.get("text") or v.get("value") or ""
        try:
            conf = float(v.get("confidence", 0.7))
        except Exception:  # noqa: BLE001
            conf = 0.7
        return str(text), max(0.0, min(1.0, conf))
    return str(v if v is not None else ""), 0.7


def parse(raw) -> dict[str, str]:
    """把模型给的 profile 字段洗干净。

    兼容三种输入：已经是 dict、JSON 字符串、以及模型不听话给的一段纯文本
    （纯文本会被丢弃——宁可这一轮不更新，也不要往固定字段里塞散文）。
    """
    data = raw
    if isinstance(raw, str):
        s = raw.strip()
        if not s.startswith("{"):
            return {}
        try:
            data = json.loads(s)
        except Exception:
            return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, str] = {}
    for k in FIELDS:
        v = data.get(k)
        if v is None:
            continue
        text, _conf = _value(v)
        if isinstance(v, (list, tuple)):
            text = "、".join(str(x).strip() for x in v if str(x).strip())
        text = " ".join(text.split()).strip()
        if text and text not in ("无", "暂无", "（无）", "-"):
            out[k] = text[:FIELD_MAX]
    return out


def parse_conf(raw) -> dict[str, float]:
    """每个字段的置信度。模型没给就按 0.7 算（中性偏可信）。"""
    data = raw
    if isinstance(raw, str):
        s_ = raw.strip()
        if not s_.startswith("{"):
            return {}
        try:
            data = json.loads(s_)
        except Exception:  # noqa: BLE001
            return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, float] = {}
    for k in FIELDS:
        if k in data and parse({k: data[k]}):
            out[k] = _value(data[k])[1]
    return out


def merge(new: dict[str, str], old: dict[str, str]) -> dict[str, str]:
    """新值覆盖旧值；模型这次没提到的字段保留上一次的。

    不整份替换：一天没聊工作，不代表他不在找工作。整份替换会把
    上个月的信息一天天擦掉。
    """
    out = dict(old or {})
    for k, v in (new or {}).items():
        if k in FIELDS and v:
            out[k] = v
    return out


def render_md(fields: dict[str, str]) -> str:
    """人读版本，也是 `self_profile.md` 的内容（review 会把它读回去）。"""
    lines = [f"- {k}：{fields[k]}" for k in FIELDS if fields.get(k)]
    return "\n".join(lines).strip()


def compact(fields: dict[str, str], conf: dict[str, float] | None = None,
            seen: dict[str, float] | None = None,
            max_chars: int = COMPACT_MAX, min_conf: float = MIN_CONF,
            now: float | None = None) -> str:
    """注入用的一行。按字段顺序拼，超长就从尾部丢字段，不截半句。

    置信度不够的字段直接不注入——宁可少说一句，也不要每轮都念错一条。
    """
    parts = []
    for k in FIELDS:
        v = fields.get(k)
        if not v:
            continue
        base = float(conf.get(k, 0.7)) if conf is not None else 1.0
        days = age_days((seen or {}).get(k), now)
        if effective_conf(base, days) < min_conf:
            continue
        parts.append(v)
    out = ""
    for p in parts:
        cand = (out + "｜" + p) if out else p
        if len(cand) > max_chars:
            break
        out = cand
    return out


def load_conf(path: Path) -> dict[str, float]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: float(v) for k, v in (data.get("confidence") or {}).items()
            if k in FIELDS}


def parse_evidence(raw) -> dict[str, str]:
    """每个字段的证据：今天对话里的原话片段。空 = 今天没提到，不算新证据。"""
    data = raw
    if isinstance(raw, str):
        t = raw.strip()
        if not t.startswith("{"):
            return {}
        try:
            data = json.loads(t)
        except Exception:  # noqa: BLE001
            return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, str] = {}
    for k in FIELDS:
        v = data.get(k)
        if isinstance(v, dict):
            ev = " ".join(str(v.get("evidence") or "").split()).strip()
            if ev and ev not in ("无", "暂无", "-"):
                out[k] = ev[:80]
    return out


def parse_changed(raw) -> dict[str, bool]:
    """模型对每个字段的判断：今天是**新信息/修正**（true）还是只是在复述（false）。

    没给（旧格式）就返回空字典，按“有变化才写”处理，不破坏兼容。
    """
    data = raw
    if isinstance(raw, str):
        t = raw.strip()
        if not t.startswith("{"):
            return {}
        try:
            data = json.loads(t)
        except Exception:  # noqa: BLE001
            return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, bool] = {}
    for k in FIELDS:
        v = data.get(k)
        if isinstance(v, dict) and isinstance(v.get("changed"), bool):
            out[k] = bool(v["changed"])
    return out


def load_previous(path: Path) -> dict[str, dict]:
    """每个字段上一次的文本和改动时间（只留最近一次）。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, dict] = {}
    for k, v in (data.get("previous") or {}).items():
        if k not in FIELDS or not isinstance(v, dict):
            continue
        text = str(v.get("text") or "").strip()
        try:
            at = float(v.get("at") or 0)
        except Exception:  # noqa: BLE001
            at = 0.0
        if text:
            out[k] = {"text": text, "at": at}
    return out


def update_state(fields: dict[str, str], conf: dict[str, float],
                 seen: dict[str, float], previous: dict[str, dict],
                 new_fields: dict[str, str], new_conf: dict[str, float],
                 evidence: dict[str, str], changed: dict[str, bool],
                 now: float, drop_after_days: float = DROP_AFTER_DAYS) -> dict:
    """把一次复盘的结果并进画像。纯函数，写入规则可以离线测。

    三条规则，对应三种真实情况：

    1. **模型说 changed=false（只是复述）**：不改文本。
       它照着旧画像换了个说法，改过去只会让画像越改越飘；只按它给的置信度记一笔，
       有今天的证据就刷新 seen（复述也能证明这条现在仍然成立）。
    2. **模型说 changed=true（新信息/修正）**：写新文本，旧的进 previous（带时间）。
       这是“慢慢改变能跟上”的唯一通道——变化必须有出处，能回看是什么时候改的。
    3. **没给 changed（旧格式）**：文本变了就按变化处理，和以前一致。

    seen 只在「有今天的证据」或「确实变了」时刷新：没有证据也没变，就让它自然过期。
    """
    f = dict(fields or {})
    c = dict(conf or {})
    s = dict(seen or {})
    prev = {k: dict(v) for k, v in (previous or {}).items()}
    dropped: list[str] = []

    for k, text in (new_fields or {}).items():
        if k not in FIELDS:
            continue
        old = f.get(k)
        flag = (changed or {}).get(k)
        has_ev = bool((evidence or {}).get(k))

        if old and flag is False:
            if k in (new_conf or {}):
                c[k] = float(new_conf[k])
            if has_ev:
                s[k] = now
            continue

        if old and text == old:
            if k in (new_conf or {}):
                c[k] = float(new_conf[k])
            if has_ev:
                s[k] = now
            continue

        if old:
            prev[k] = {"text": old, "at": now}
        f[k] = text
        if k in (new_conf or {}):
            c[k] = float(new_conf[k])
        if has_ev or flag:
            s[k] = now

    # 首次迁移：给还没有时钟的字段起一个，别让老数据一上来就过期
    for k in list(f):
        s.setdefault(k, now)

    # 过期：太久没有新证据，从库里删掉（不只是不注入）
    for k in list(f):
        d = age_days(s.get(k), now)
        if d is not None and d > drop_after_days:
            f.pop(k, None)
            c.pop(k, None)
            s.pop(k, None)
            prev.pop(k, None)
            dropped.append(k)

    return {"fields": f, "confidence": c, "seen": s,
            "previous": prev, "dropped": dropped}


def age_days(seen: float | None, now: float | None = None) -> float | None:
    if not seen:
        return None
    now = time.time() if now is None else now
    return max(0.0, (now - float(seen)) / 86400.0)


def effective_conf(conf: float, days: float | None) -> float:
    """到期就是 0（不注入）。没过期就照原样——不做渐进衰减，规则要能一句话说清。"""
    if days is None:
        return float(conf)
    return 0.0 if days > DROP_AFTER_DAYS else float(conf)


def load_seen(path: Path) -> dict[str, float]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: float(v) for k, v in (data.get("seen") or {}).items() if k in FIELDS}


def load(path: Path) -> dict[str, str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return parse(data.get("fields") if isinstance(data, dict) else data)


def save(path: Path, fields: dict[str, str], conf: dict[str, float] | None = None,
         seen: dict[str, float] | None = None,
         previous: dict[str, dict] | None = None,
         extra: dict | None = None) -> None:
    payload = {"fields": {k: fields[k] for k in FIELDS if fields.get(k)}}
    if conf:
        payload["confidence"] = {k: round(float(conf[k]), 2)
                                 for k in FIELDS if fields.get(k) and k in conf}
    if seen:
        payload["seen"] = {k: float(seen[k]) for k in FIELDS
                           if fields.get(k) and k in seen}
    if previous:
        payload["previous"] = {
            k: {"text": str(previous[k].get("text") or ""),
                "at": float(previous[k].get("at") or 0)}
            for k in FIELDS
            if fields.get(k) and k in previous and (previous[k] or {}).get("text")}
    if extra:
        payload.update(extra)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")


def forget(path: Path, key: str) -> bool:
    """删掉一个字段（连同置信度和时间戳）。返回有没有删到。"""
    fields = load(path)
    if key not in fields:
        return False
    conf = load_conf(path)
    seen = load_seen(path)
    prev = load_previous(path)
    fields.pop(key, None)
    conf.pop(key, None)
    seen.pop(key, None)
    prev.pop(key, None)
    save(path, fields, conf, seen, prev,
         extra={"updated_at": time.time(), "cleared": key})
    return True


def clear(path: Path) -> None:
    """整份清空。下次复盘会从零重新长，不会立刻填满。"""
    save(path, {}, {}, {}, {}, extra={"updated_at": time.time(), "cleared": "all"})
