#!/usr/bin/env python3
"""审批端口契约：请求/结果，不绑定 QQ 或按钮。"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CODE = HERE.parent / "nonebot"
if not (CODE / "app_core").is_dir():
    CODE = HERE.parent
sys.path.insert(0, str(CODE))

from app_core.approval import ApprovalRequest, ApprovalResult  # noqa: E402

PASS = FAIL = 0


def check(name, ok, extra=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("PASS " + name)
    else:
        FAIL += 1
        print("FAIL " + name + (" | " + str(extra) if extra else ""))


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

    print(f"\n=== {PASS}/{PASS + FAIL} 通过 ===")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
