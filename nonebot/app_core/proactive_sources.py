"""主动消息的候选来源：只负责取数 + 归一化，不决定发不发。

proactive.py 是纯判断；这里把 followup / curious / memory 三种存储的
取数细节收在一处，插件只负责调度和发送。
"""
from __future__ import annotations

import hashlib
import time

from app_core import followup, learn


def followup_candidates(limit: int = 5, now: float | None = None) -> list[tuple]:
    now = time.time() if now is None else now
    out = []
    try:
        for it in followup.due_for_asking(limit):
            days = max(0, int((now - float(it["created"] or 0)) / 86400))
            when = "今天" if days == 0 else f"约 {days} 天前"
            out.append((f"fu:{it['id']}", it["text"],
                        f"{when}他说要做的事，顺口问问进展"))
    except Exception:
        return []
    return out


def curious_candidates() -> list[tuple]:
    out = []
    for x in learn.read_list(learn.CURIOUS):
        try:
            if learn.curious_ok(x):
                out.append(("cur:" + hashlib.sha1(x.encode("utf-8")).hexdigest()[:8],
                            x, "之前没讲清楚的事（可能有一阵子了），顺口问一句"))
        except Exception:
            continue
    return out


def memory_candidates(rows: list[dict] | None) -> list[tuple]:
    out = []
    for r in rows or []:
        try:
            doc_id = r["doc_id"]
            text = (r.get("text") or "")[:120]
            if text:
                out.append((f"mem:{doc_id}", text, "突然想起这件事，有阵子没提了"))
        except Exception:
            continue
    return out


def collect(memory_forgotten) -> tuple[list, list, list]:
    """给插件用的总入口；memory_forgotten 由调用方注入，避免反向依赖 chat。

    2026-11-10 按真实使用收窄：只返回 followup。11 天发了 5 条主动消息，
    curious 3 条 + memory 1 条全部没被回复，唯一接住的是 followup。
    两个函数保留，想恢复只改这一行；memory_forgotten 参数也为兼容保留。
    """
    return followup_candidates(), [], []
