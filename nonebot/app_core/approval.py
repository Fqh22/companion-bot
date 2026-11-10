"""审批端口：引擎只描述“要批准什么”，怎么呈现交给适配器。

QQ 现在是 /approve 命令；以后可以有按钮/文本 y/n。
Engine 不关心传输，只拿 ApprovalResult。
"""
from __future__ import annotations

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
