#!/usr/bin/env python3
"""审批端口契约：请求/结果，不绑定 QQ 或按钮。"""
from __future__ import annotations

import asyncio

from harness import bootstrap, check, report

bootstrap()

from app_core.approval import (  # noqa: E402
    ApprovalRequest, ApprovalResult, CommandApprovalPort)


class FakeApproval:
    def __init__(self, result):
        self.result = result
        self.requests = []

    async def ask(self, req):
        self.requests.append(req)
        return self.result


async def main():
    req = ApprovalRequest("a1", "执行危险命令", "rm -rf /tmp/x")
    check("请求默认选项", req.options == ("approve", "reject"), req.options)
    check("请求字段完整", req.approval_id == "a1" and "危险" in req.title)

    port = FakeApproval(ApprovalResult(True, "approve", "console"))
    res = await port.ask(req)
    check("批准结果", res.approved and res.choice == "approve", res)
    check("端口收到请求", port.requests == [req], port.requests)

    deny = await FakeApproval(ApprovalResult(False, "reject")).ask(req)
    check("拒绝结果", not deny.approved and deny.choice == "reject", deny)

    port2 = CommandApprovalPort()
    task = asyncio.create_task(port2.ask(ApprovalRequest("a2", "执行危险命令")))
    await asyncio.sleep(0)
    check("命令端口登记 pending", port2.pending_ids() == ["a2"], port2.pending_ids())
    check("命令端口 resolve", port2.resolve("a2", True, "approve", "qq"))
    got = await task
    check("命令端口返回结果", got.approved and got.by == "qq", got)
    check("重复/未知 resolve 返回 False", not port2.resolve("a2", True))
    check("完成后无 pending", port2.pending_ids() == [])


if __name__ == "__main__":
    asyncio.run(main())
    raise SystemExit(report())
