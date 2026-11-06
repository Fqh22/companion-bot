"""提醒门面：实现搬到 app_core/scheduling/remind.py。"""
from __future__ import annotations

from app_core import paths as _paths
from app_core.scheduling.remind import (  # noqa: F401
    MAX_DAYS, MAX_TEXT, _conn, _fmt, _norm, _parse, _similar, add, cancel,
    claim, describe_pending, due_now, init, list_pending, mark_sent, release,
)

DB = _paths.DATA / "chat.db"
