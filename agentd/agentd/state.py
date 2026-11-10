"""任务状态机：只允许明确的状态迁移。

循环里仍可调用 store.set_task 做兼容；新代码走 transition_task，
把 needs_input / done / failed 的路径固定下来。
"""
from __future__ import annotations

from typing import Any

PENDING = "pending"
RUNNING = "running"
NEEDS_INPUT = "needs_input"
DONE = "done"
FAILED = "failed"
PAUSED = "paused"
CANCELLED = "cancelled"

STATES = {PENDING, RUNNING, NEEDS_INPUT, DONE, FAILED, PAUSED, CANCELLED}

TRANSITIONS = {
    PENDING: {RUNNING, CANCELLED},
    RUNNING: {NEEDS_INPUT, DONE, FAILED, PAUSED, CANCELLED},
    NEEDS_INPUT: {PENDING, FAILED, CANCELLED},
    PAUSED: {PENDING, CANCELLED},
    FAILED: {PENDING},
    DONE: {PENDING},
    CANCELLED: {PENDING},
}


def can_transition(src: str, dst: str) -> bool:
    return src == dst or dst in TRANSITIONS.get(src, set())


def transition_task(store: Any, task_id: int, dst: str, **kw: Any) -> None:
    """迁移任务状态；非法迁移直接抛 ValueError，避免静默卡死。"""
    if dst not in STATES:
        raise ValueError(f"未知状态：{dst}")
    cur = (store.task(task_id) or {}).get("status")
    if cur and not can_transition(str(cur), dst):
        raise ValueError(f"非法状态迁移：{cur} -> {dst}")
    store.set_task(task_id, status=dst, **kw)
