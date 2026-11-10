"""审批端口：引擎只描述“要批准什么”，怎么呈现交给适配器。

QQ 现在是 /approve 命令；以后可以有按钮/文本 y/n。
Engine 不关心传输，只拿 ApprovalResult。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ApprovalRequest:
    approval_id: str
    title: str
    detail: str = ""
    options: tuple[str, ...] = ("approve", "reject")


@dataclass(frozen=True)
class ApprovalResult:
    approved: bool
    choice: str = ""
    by: str = ""


class ApprovalPort(Protocol):
    async def ask(self, req: ApprovalRequest) -> ApprovalResult: ...


class CommandApprovalPort:
    """内存实现：ask 挂起，/approve 或 /reject 命令来 resolve。

    当前 agentd 自己走 HTTP approval，不需要 Engine 阻塞等待；
    这个实现是给未来“引擎产生审批”或 console 适配器用的。
    """

    def __init__(self) -> None:
        self._pending: dict[str, asyncio.Future] = {}

    async def ask(self, req: ApprovalRequest) -> ApprovalResult:
        loop = asyncio.get_running_loop()
        if req.approval_id in self._pending:
            raise ValueError("approval_id 已存在：" + req.approval_id)
        fut: asyncio.Future = loop.create_future()
        self._pending[req.approval_id] = fut
        try:
            return await fut
        finally:
            self._pending.pop(req.approval_id, None)

    def resolve(self, approval_id: str, approved: bool,
                choice: str = "", by: str = "") -> bool:
        fut = self._pending.get(approval_id)
        if fut is None or fut.done():
            return False
        fut.set_result(ApprovalResult(approved, choice, by))
        return True

    def pending_ids(self) -> list[str]:
        return list(self._pending)
