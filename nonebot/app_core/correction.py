"""[orchestration] 纠正分诊：当场识别，后台判断，三类去处。

事实纠正 → MemoryPort（range_key 按 claim 哈希幂等 + 矛盾检测）；
行为纠正 → corrections.md；
提不出来但像事实 → corrections_pending.jsonl（永不注入，等复盘）。

见 docs/16。这里只做「判断 + 分流」，不阻塞回合。
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time

from app_core import paths as _paths

PENDING = _paths.DATA / "corrections_pending.jsonl"
UNVERIFIED = _paths.DATA / "corrections_unverified.jsonl"
STATE = _paths.DATA / "review_state.json"
log = logging.getLogger("qqbot.correction")

SYSTEM = (
    "判断用户这句话是不是在纠正你上一轮的回复。只输出 JSON："
    '{"is_correction":true|false,"kind":"fact|behavior|none",'
    '"claim":"一句带主语「他」的完整断言，提不出就留空",'
    '"confidence":0.0,"why":"不超过 20 字"}'
    "只有他明确否定你刚才说的内容才算纠正；讨论里说「不对」但不是说你，不算。"
    "fact=可跨会话成立的事实断言（生日/城市/价格/偏好）；"
    "behavior=对你怎么说话的要求（别只顺着说/别猜）。"
)


def _json(text: str) -> dict:
    t = (text or "").strip()
    i, j = t.find("{"), t.rfind("}")
    if i < 0 or j <= i:
        return {}
    try:
        d = json.loads(t[i:j + 1])
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _hash(claim: str) -> str:
    norm = " ".join((claim or "").split()).lower()
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:16]


def _append(path, rec) -> None:
    try:
        _paths.DATA.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + chr(10))
    except Exception as e:  # noqa: BLE001
        log.debug("写 %s 失败：%s", path.name, e)


def _read(path) -> list[dict]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        return []
    out = []
    for ln in lines:
        try:
            out.append(json.loads(ln))
        except Exception:  # noqa: BLE001
            continue
    return out


def _rewrite(path, rows: list[dict]) -> None:
    try:
        _paths.DATA.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(r, ensure_ascii=False) + chr(10) for r in rows),
                        encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        log.debug("重写 %s 失败：%s", path.name, e)


def _state() -> dict:
    try:
        d = json.loads(STATE.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _save_state(d: dict) -> None:
    try:
        _paths.DATA.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def looks_like_correction(text: str) -> bool:
    from app_core.mood_domain import CORRECTION_HINTS

    t = (text or "").lower()
    return any(k in t for k in CORRECTION_HINTS)


async def adjudicate(core, user_text: str, prev_reply: str, rid: str = "",
                     session: str = "", write_pending: bool = True) -> str:
    """后台判断一次，返回 fact / behavior / pending / none。"""
    if not (user_text or "").strip() or not (prev_reply or "").strip():
        return "none"
    try:
        msg = await core.complete_raw(
            [{"role": "system", "content": SYSTEM},
             {"role": "user", "content":
              "你上一轮说：" + prev_reply[:500] + chr(10) + "他这轮说：" + user_text[:500]}],
            max_tokens=200, thinking=False, purpose="correction")
        data = _json(msg.get("content") or "")
    except Exception as e:  # noqa: BLE001
        log.debug("纠正判断失败：%s", e)
        return "none"
    if not data.get("is_correction"):
        return "none"
    kind = str(data.get("kind") or "").strip()
    claim = str(data.get("claim") or "").strip()[:200]
    try:
        conf = float(data.get("confidence") or 0)
    except Exception:  # noqa: BLE001
        conf = 0.0
    if kind == "fact" and claim and conf >= 0.7:
        try:
            port = core._memory_port() if hasattr(core, "_memory_port") else None
            if port is not None:
                from app_core.memory_port import MemoryItem

                await port.remember(MemoryItem(
                    type="fact", text=claim, confidence=conf, source="correction",
                    payload={"range_key": "corr:" + _hash(claim), "session": session}))
                return "fact"
        except Exception as e:  # noqa: BLE001
            log.warning("纠正写入记忆失败：%s", e)
    if kind == "behavior" and claim:
        try:
            from app_core import learn

            learn.merge_list(learn.CORRECTIONS, "他纠正过我的", [claim])
            return "behavior"
        except Exception as e:  # noqa: BLE001
            log.warning("纠正写入行为清单失败：%s", e)
    if write_pending:
        _append(PENDING, {"ts": time.time(), "rid": rid, "session": session,
                          "user_text": user_text[:500], "prev_reply": prev_reply[:500],
                          "kind": kind or "unknown", "claim": claim,
                          "confidence": conf, "tries": 0})
    return "pending"


async def process_pending(core, limit: int = 5) -> int:
    """复盘时重试 pending；失败 3 次移到 unverified（永不注入）。"""
    rows = _read(PENDING)
    if not rows:
        return 0
    last = float(_state().get("last_pending_ts") or 0)
    due = [r for r in rows if float(r.get("ts") or 0) > last][:max(1, int(limit))]
    if not due:
        return 0
    keep: list[dict] = []
    moved = 0
    for r in due:
        res = await adjudicate(core, str(r.get("user_text") or ""),
                               str(r.get("prev_reply") or ""), str(r.get("rid") or ""),
                               str(r.get("session") or ""), write_pending=False)
        if res == "pending":
            r["tries"] = int(r.get("tries") or 0) + 1
            if r["tries"] >= 3:
                _append(UNVERIFIED, r)
                moved += 1
            else:
                keep.append(r)
    rest = [r for r in rows if r not in due]
    _rewrite(PENDING, rest + keep)
    _save_state({"last_pending_ts": max(float(r.get("ts") or 0) for r in due)})
    return len(due) - moved