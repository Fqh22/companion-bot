"""[orchestration] 此刻：把「他最近在忙什么、还挂着什么、情绪如何」收成一小段。

为什么不是记忆召回：记忆是**事实**（会 SQL、在找工作），这里是**状态**（今天在弄简历、
还挂着面试、情绪偏低）。事实能说明他是谁，状态才能说明他此刻在哪——「接住」靠的是后者。

成本：每轮读最近消息 + 待办 + 情绪，约 100 字进 prompt。
"""
from __future__ import annotations

import sqlite3
import time

from app_core import followup, mood, paths

MAX_TOPICS = 3
TOPIC_DAYS = 3.0


def _clean_topic(text: str) -> str:
    return (text or "").strip().replace("[语音] ", "")


def recent_topics(session: str, limit: int = MAX_TOPICS, latest: str = "") -> list[str]:
    """最近几天他说过的话（短句），去重、截短。只读。

    latest：本轮他刚说的那句。user 消息现在在 POST_WRITE 才落库，
    读 DB 已经看不到它了——用它补上第一条，行为与「写库在 prepare」时一致。
    """
    since = time.time() - TOPIC_DAYS * 86400
    try:
        c = sqlite3.connect(f"file:{paths.CHAT_DB}?mode=ro", uri=True, timeout=10)
        rows = c.execute(
            "SELECT content, ts FROM messages WHERE role='user' AND session=? "
            "ORDER BY id DESC LIMIT 30", (session,)).fetchall()
        c.close()
    except Exception:  # noqa: BLE001
        return []
    out: list[str] = []
    _lt = _clean_topic(latest)
    if _lt and len(_lt) >= 4 and not _lt.startswith("/"):
        out.append(_lt[:14])
    for text, ts in rows:
        try:
            if float(ts or 0) < since:
                continue
        except Exception:  # noqa: BLE001
            continue
        t = _clean_topic(text)
        if not t or len(t) < 4 or t.startswith("/"):
            continue
        if any(t[:8] in x for x in out):
            continue
        out.append(t[:14])
        if len(out) >= limit:
            break
    return out


def describe(session: str, m: dict | None = None, latest: str = "") -> str:
    """一段「此刻」；没有可说的就返回空串（不注入）。"""
    parts: list[str] = []
    topics = recent_topics(session, latest=latest)
    if topics:
        parts.append("最近在聊：" + "、".join(topics))
    try:
        todo = [x["text"][:18] for x in followup.pending(2)]
        if todo:
            parts.append("还挂着：" + "；".join(todo))
    except Exception:  # noqa: BLE001
        pass
    try:
        if m:
            parts.append("此刻：" + mood.describe(m).splitlines()[0][:60])
    except Exception:  # noqa: BLE001
        pass
    return "\n".join(parts)
