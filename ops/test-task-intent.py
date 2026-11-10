#!/usr/bin/env python3
"""任务意图规则：只在明确时升级，模糊句留给聊天。"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CODE = HERE.parent / "nonebot"
if not (CODE / "app_core").is_dir():
    CODE = HERE.parent
sys.path.insert(0, str(CODE))

from app_core import task_intent as ti  # noqa: E402

PASS = FAIL = 0


def check(name, ok, extra=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("PASS " + name)
    else:
        FAIL += 1
        print("FAIL " + name + (" | " + str(extra) if extra else ""))


def main():
    check("明确交给 agent", ti.is_task_request("交给 agent：把简历改成一页"))
    check("文件 + 动作", ti.is_task_request("帮我把这份简历重排", has_file=True))
    check("无文件 + 动作也拦", ti.is_task_request("帮我生成一个 CSV"))
    check("普通问句不拦", not ti.is_task_request("你觉得这个怎么样"))
    check("带文件纯问句不拦", not ti.is_task_request("这是什么", has_file=True))
    check("开始/不用", ti.is_start("开始") and ti.is_cancel("不用"))
    check("确认里有附件名", "简历.pdf" in ti.confirm_text("改简历", ["/tmp/简历.pdf"]))
    check("无附件有提示", "无（" in ti.confirm_text("改简历", []))
    check("显式 goal 去掉前缀", ti.explicit_goal("交给 agent：做 A") == "A")
    print(f"\n=== {PASS}/{PASS + FAIL} 通过 ===")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
