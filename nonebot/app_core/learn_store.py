"""[orchestration] 学习层的存储：路径、画像字段、清单读写、复盘落盘。

从 learn.py 抽出来（2026-11-16）的原因：review.py 需要 profile / 清单 / apply_review
这一整组数据访问，而 learn 又要 import review 做复盘入口——不抽这一层就是双向环。
边界：只做「文件 / 字段的读写与文本规则」，不调模型、不写 wiki 索引。

learn.py 保留业务：技能、笔记写入（带 _touch_wiki）、间隔重复、归档、复盘入口。
"""
from __future__ import annotations

import json
import logging
import math
import re
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

from app_core import paths as _paths
from app_core import profile as profile_mod

log = logging.getLogger("qqbot.learn")

# 纠正清单的解析缓存：按 (路径, mtime, 大小) 复用，避免每轮重读文件
_CORR_CACHE: dict[tuple, list[str]] = {}

LEARN = _paths.LEARN
PROFILE = LEARN / "self_profile.md"

PROFILE_JSON = LEARN / "profile.json"
CORRECTIONS = LEARN / "corrections.md"
CURIOUS = LEARN / "curious.md"
SKILLS = LEARN / "skills"
NOTES = LEARN / "notes"
RAW = LEARN / "raw"          # 对话原文（原始材料，不加工）
MEM_DB = _paths.MEM_DB
CHAT_DB = _paths.DATA / "chat.db"

def profile_fields() -> dict[str, str]:
    """当前结构化画像。文件不在（还没跑过复盘）就返回空。"""
    return profile_mod.load(PROFILE.parent / "profile.json")

def profile_conf() -> dict[str, float]:
    return profile_mod.load_conf(PROFILE.parent / "profile.json")

def profile_seen() -> dict[str, float]:
    return profile_mod.load_seen(PROFILE.parent / "profile.json")

def profile_path() -> Path:
    return PROFILE.parent / "profile.json"

def profile_previous() -> dict[str, dict]:
    return profile_mod.load_previous(PROFILE.parent / "profile.json")

def read_list(path: Path) -> list[str]:
    try:
        if path.exists():
            return [ln[2:].strip() for ln in path.read_text(encoding="utf-8").splitlines()
                    if ln.startswith("- ")]
    except Exception:
        pass
    return []

def _clean_key(s: str) -> str:
    """去掉场景标签和标点。**标签必须一起去掉**——不去的话
    「[文字]他明确」这种跨标签的套话会把三条不相干的教训连成一串。"""
    s = re.sub(r"^\s*\[(文字|图片|通用)\]\s*", "", str(s or ""))
    return re.sub(r"[\s，。、；：！？「」“”\"'（）()\-—…~,.;:!?]+", "", s)

def _grams(s: str, n: int) -> set[str]:
    return {s[i:i + n] for i in range(len(s) - n + 1)} if len(s) >= n else set()

def dedupe_items(items: list[str], min_len: int = 6, max_df: int = 3) -> list[str]:
    """同一条教训被换个说法写了几遍时，只留最近的那一条。

    **判据不是「句子像不像」，而是「有没有共享一个罕见的长短语」。**
    实测那三条「谎称记着了」的句子相似度只有 0.31 / 0.43 / 0.59，任何落在
    合理区间的阈值（≥0.55 才不误合别的事）都抓不到；但它们都含
    「记着了但实际」「说记就必须真」这类只出现两三次的 6 字以上短语。

    反过来，「他明确说」「他嫌我」「他明确嫌我」这种套话也会被判共享，
    所以卡两道：短语至少 `min_len` 个字、且全局只出现在 `max_df` 条以内。
    在线上 16 条真实数据上，这组参数合并 4 组、没有误合。
    """
    normed = [_clean_key(x) for x in items]
    n = len(items)
    if n < 2:
        return list(items)

    # 只收长度够、出现次数少的短语，倒排到条目上
    df: dict[str, int] = {}
    where: dict[str, list[int]] = {}
    for i, s in enumerate(normed):
        seen: set[str] = set()
        for L in range(min_len, min(len(s), 12) + 1):
            seen |= _grams(s, L)
        for g in seen:
            df[g] = df.get(g, 0) + 1
            where.setdefault(g, []).append(i)

    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for g, idx in where.items():
        if len(idx) < 2 or df[g] > max_df:
            continue
        base = idx[0]
        for other in idx[1:]:
            union(base, other)

    # 每个簇只留最后出现的那条（最近的写法）
    keep: dict[int, int] = {}
    for i in range(n):
        keep[find(i)] = i
    keep_set = set(keep.values())
    return [items[i] for i in range(n) if i in keep_set]

def recent_corrections(limit: int = 6, scene: str | None = None) -> list[str]:
    """他最近纠正过我的事。

    这些以前只写进文件、复盘时用来自查，**聊天时根本看不到**——所以纠正了一个月
    行为也不变。现在每轮注入 prompt（见 prompt.build_messages 的 lessons 段）。

    scene：按场景过滤。`image` 只取图片相关+通用，`text` 只取文字相关+通用。
    不分场景的话，「别瞎猜糊图」会被塞进纯文字对话里，占预算又没用——图片类
    提醒只在真有图的那轮才有意义。

    注入前先做一次罕见短语去重：6 个槽位里塞 3 条「别谎称记住」等于浪费一半。
    结果按文件 mtime 缓存——这份清单一天只变一次（03:40 复盘），
    每轮重算 16 条的两两比较纯属浪费。
    """
    try:
        mtime = CORRECTIONS.stat().st_mtime
    except Exception:
        mtime = 0.0
    key = (str(CORRECTIONS), mtime, scene, int(limit))
    hit = _CORR_CACHE.get(key)
    if hit is not None:
        return list(hit)

    items = read_list(CORRECTIONS)
    if scene:
        items = [it for it in items if correction_scene(it) in ("general", scene)]
    items = dedupe_items(items)
    out = items[-max(1, int(limit)):]
    _CORR_CACHE.clear()
    _CORR_CACHE[key] = list(out)
    return out

def correction_scene(item: str) -> str:
    """读一条纠正的场景标签。没标签的老条目按「通用」算。"""
    t = (item or "").strip()
    for tag, scene in (("[图片]", "image"), ("[文字]", "text"), ("[通用]", "general")):
        if t.startswith(tag):
            return scene
    return "general"

def write_list(path: Path, title: str, items: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"# {title}\n\n" + "\n".join(f"- {x}" for x in items) + "\n", encoding="utf-8")

def _list_key(item: str) -> str:
    """去掉场景标签和标点，只留用于比较的骨架。"""
    t = re.sub(r"^[\[【]\s*(图片|文字|通用)\s*[\]】]", "", (item or "").strip())
    return re.sub(r"[，。、；：！？：「」“”\"'（）()【】\[\]\s]", "", t)

def too_similar(a: str, b: str, threshold: float = 0.62) -> bool:
    """两条是不是在说同一件事。

    复盘每天写 3-5 条，模型很爱换句话重复同一个意思（「记着了」那件事写过两遍）。
    精确匹配拦不住，纯靠 cap 又会把真教训挤出去，所以按相似度合并。

    两道保险，避免把不同的事误合：
    - 长度差太多不比较（短句容易被长句"吸收"）；
    - 阈值 0.62 是在线上 14 条上试出来的，低于它会开始吃掉不同的条目。
    """
    import difflib

    ka, kb = _list_key(a), _list_key(b)
    if not ka or not kb:
        return False
    if ka == kb:
        return True
    short, long_ = sorted((len(ka), len(kb)))
    # 短条目只认精确匹配：「问题三」和「问题一」相似度就够高，但它们是两件事。
    # 模糊匹配只对够长的句子有意义，那里才有足够的语义信息。
    if short < 8:
        return ka == kb
    if long_ and short / long_ < 0.55:
        return False
    return difflib.SequenceMatcher(None, ka, kb).ratio() >= threshold

def merge_list(path: Path, title: str, items: list[str], cap: int = 24) -> list[str]:
    """合并进一个列表文件，保留最近 cap 条。

    上限一路从 10 提到 24：端到端跑一次复盘就新增 5 条，10 的容量两天就满，
    而最先被丢掉的往往是最早也最关键的几条（「别只顺着我说话」）。
    注入时只取 6 条，所以存得多不等于贵。

    合并规则：**相似即重复**。不光比较新来的，整表也重新去一次重——
    这样历史上已经写重的条目会在下一次写入时自动收敛。
    """
    cur = read_list(path)
    for it in items:
        it = (it or "").strip()[:120]
        if it and not any(too_similar(it, x) for x in cur):
            cur.append(it)
    deduped: list[str] = []
    for it in cur:
        if not any(too_similar(it, x) for x in deduped):
            deduped.append(it)
    if len(deduped) < len(cur):
        log.info("%s 合并了 %d 条相似条目", path.name, len(cur) - len(deduped))
    cur = deduped
    if len(cur) > cap:
        dropped = len(cur) - cap
        log.warning("列表 %s 超出上限，丢弃最旧 %d 条", path.name, dropped)
    cur = cur[-cap:]
    write_list(path, title, cur)
    return cur

_CURIOUS_BAD = re.compile(
    r"还没(提过|讲过|说过|确认|答|说)|没提过|没讲过|没说过|没说|一直没|没定|"
    r"不清楚|不知道|未确认|尚未")

def curious_ok(text: str) -> bool:
    """这条「没搞懂的事」值不值得以后主动问。

    线上清单里混进过「他的家庭情况还没提过」「投技术还是非技术一直没讲」——
    那是信息缺口，不是他没讲清楚的问题，问出来只会让人不舒服。
    """
    t = (text or "").strip()
    if not t or len(t) > 120:
        return False
    return not _CURIOUS_BAD.search(t)

def resolve_curious(items: list[str]) -> int:
    cur = read_list(CURIOUS)
    if not cur or not items:
        return 0

    def hit(line: str) -> bool:
        return any(it and (it in line or line in it) for it in items)

    remain = [x for x in cur if not hit(x)]
    n = len(cur) - len(remain)
    if n:
        write_list(CURIOUS, "还没搞懂的事", remain)
    return n

def normalize_correction(item: str) -> str:
    """给一条纠正补场景标签。

    复盘模型**可能漏标**。漏了就按「通用」存，而不是留成无标签——无标签在读取时
    会退化成通用、两类都注入，等于分流失效。写入时补上，分流才是确定的。
    """
    t = (item or "").strip()[:120]
    if not t:
        return ""
    if re.match(r"^[\[【]\s*(图片|文字|通用)", t):
        return t
    return "[通用] " + t

def save_profile(fields: dict, conf: dict, evidence: dict,
                 changed: dict | None = None, day: str | None = None) -> dict:
    """把画像字段合并进 profile.json + self_profile.md。**纯文件写入，不调模型**。

    从 apply_review 里抽出来，给 MemoryPort 用（P5）：写入规则只有一份，
    复盘和契约层都调这里。行为与原来完全一致。
    """
    if not fields:
        return {"dropped": []}
    json_path = PROFILE.parent / "profile.json"
    now = time.time()
    state = profile_mod.update_state(
        profile_mod.load(json_path), profile_mod.load_conf(json_path),
        profile_mod.load_seen(json_path), profile_mod.load_previous(json_path),
        fields, conf, evidence, changed or {}, now)
    merged = state["fields"]
    profile_mod.save(json_path, merged, state["confidence"], state["seen"],
                     state["previous"],
                     extra={"updated_at": now, "day": day})
    PROFILE.write_text(profile_mod.render_md(merged), encoding="utf-8")
    return state

def apply_review(data: dict, day: str | None = None) -> dict:
    """把复盘产出的 JSON 落到文件。**不调模型**，所以写入规则可以离线测。

    拆出来的原因：review() 里"调模型"和"写文件"混在一起，导致写入规则
    （纠正必须带场景标签、列表上限、去重）一直没被测过——只测了读的那一侧。
    """
    day = day or datetime.now().strftime("%Y-%m-%d")
    learned = str(data.get("learned") or "").strip()
    raw_profile = data.get("profile")
    fields = profile_mod.parse(raw_profile)
    conf = profile_mod.parse_conf(raw_profile)
    evidence = profile_mod.parse_evidence(raw_profile)
    # 老格式（一段自由文本）仍然收下，写进 md；json 这边就当这轮没给
    legacy = ""
    if not fields and isinstance(raw_profile, str):
        legacy = raw_profile.strip()[:1200]
    out = {"learned": bool(learned), "profile": bool(fields or legacy),
           "corrections": 0, "curious": 0, "resolved": 0}
    if not learned and not bool(fields or legacy):
        return out
    LEARN.mkdir(parents=True, exist_ok=True)
    (LEARN / f"{day}.md").write_text(
        f"# {day} 复盘\n\n## 今天学到\n{learned}\n\n## 自我认知更新\n"
        f"{profile_mod.render_md(fields) or legacy}\n",
        encoding="utf-8")
    if fields:
        # 合并而不是整份替换：一天没聊工作，不代表他不在找工作。
        # 写入规则全在 save_profile → profile.update_state（纯函数）。
        state = save_profile(fields, conf, evidence,
                            profile_mod.parse_changed(raw_profile), day)
        if state.get("dropped"):
            out["profile_dropped"] = state["dropped"]
            log.info("[learn] 画像过期删除：%s", "、".join(state["dropped"]))
    elif legacy:
        PROFILE.write_text(legacy, encoding="utf-8")

    corr = [normalize_correction(str(c)) for c in (data.get("corrections") or [])]
    corr = [c for c in corr if c]
    if corr:
        merge_list(CORRECTIONS, "他纠正过我的", corr)
    out["corrections"] = len(corr)

    cur = [str(c).strip() for c in (data.get("curious") or []) if str(c).strip()]
    cur = [c for c in cur if curious_ok(c)]
    if cur:
        merge_list(CURIOUS, "还没搞懂的事", cur)
    out["curious"] = len(cur)

    resolved = [str(x).strip() for x in (data.get("resolved_curious") or [])
                if str(x).strip()]
    if resolved:
        out["resolved"] = resolve_curious(resolved)
    return out

def note_list(limit: int = 10) -> list[str]:
    try:
        files = sorted(NOTES.glob("*.md"), key=lambda x: x.stat().st_mtime, reverse=True)[:limit]
        return [p.name for p in files]
    except Exception:
        return []
