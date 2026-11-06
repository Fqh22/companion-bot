"""跟进事项：独立于记忆生命周期，带状态机。

为什么单独一张表，而不是塞在记忆的 metadata 里：

- 记忆会被清理归档，followup 跟着一起消失——「他之前说要做的」就再也想不起来了
- 原来只有 done 布尔值，同一件事会被反反复复问
- 提醒（remind）产生的事项也要进这里，才能被主动消息跟进

状态机（status 是权威，done 只是「已关闭」的派生标记，保留是为了索引和
所有旧查询不用改）：

    todo（待办）→ doing（进行中）→ done（已完成）
    partial（部分完成）可以继续推进；postponed（延期）到期再问
    任何未关闭状态都能 cancelled（取消）

`done` 的含义是「不用再问了」，所以 done 和 cancelled 都记 1。
"""
from __future__ import annotations

from app_core import paths as _paths
from app_core.followup_domain import (  # noqa: F401
    AFFIRM, ASK_MAX_AGE_DAYS, ASK_MAX_TIMES, CLOSED, COOLDOWN_DAYS, DONE_SIGNALS,
    DROP_SIGNALS, KEEP_MAX_OPEN, MAX_AGE_DAYS, MAX_ASKS, MAX_TEXT, NEAR_DUP,
    NEGATIONS, PARTIAL_SIGNALS, POSTPONE_SIGNALS, STATUSES,
    _overlap, _signal_hit, askable, resolve_from,
)
from app_core.followup_store import (  # noqa: F401
    _candidates, _conn, add, add_many, apply, cancel_ref, confirm_pending,
    describe_statuses, due_for_asking, expire, init, last_asked, mark_asked,
    mark_done, pending, resolve, set_status, stats, texts_for,
)

DB = _paths.DATA / "chat.db"
