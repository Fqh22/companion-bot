"""定时任务门面：实现搬到 app_core/scheduling/scheduler.py。"""
from __future__ import annotations

from app_core import paths as _paths
from app_core.scheduling.scheduler import (  # noqa: F401
    HARD_HINTS, JOBS, NL, Scheduler,
)

CHAT_DB = _paths.DATA / "chat.db"
CLEANUP_LOG = _paths.DATA / "memory-cleanup.log"
