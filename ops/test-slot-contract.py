#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""槽位契约：只验结构，不验渲染字节。

黄金语料（ops/test-prompt-golden.py）验的是「真实渲染逐字节不变」；
这里验的是「结构本身成立」：阶段机、槽序、同槽多块、只读。
两者分工，所以加用例不必动 fixtures。
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CODE = HERE.parent / "nonebot"
if not (CODE / "app_core").is_dir():
    CODE = HERE.parent
sys.path.insert(0, str(CODE))

from app_core.chat_turn import (  # noqa: E402
    PREFETCH, RENDER_SLOTS, SLOTS, StageError, Turn)

PASS = FAIL = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print("PASS " + name)
    else:
        FAIL += 1
        print("FAIL " + name + (" | " + str(extra) if extra else ""))


def mk() -> Turn:
    return Turn(session="private:t", user_text="u", images=[], store_text=None,
                owner=True, rid="r", t0=0.0)


def main() -> int:
    keys = [s.key for s in sorted(SLOTS, key=lambda x: x.sid)]

    # 1) 阶段机：PREFETCH 里写槽位必须抛
    t = mk()
    raised = False
    try:
        t.set_slot("weather", "x")
    except StageError:
        raised = True
    check("PREFETCH 阶段写槽位 → StageError", raised)

    # 2) 乱序赋值 → 按 sid 渲染（顺序不看调用时序）
    t = mk()
    t.stage = RENDER_SLOTS
    for k in reversed(keys):
        t.set_slot(k, k)
    check("乱序赋值 → 按 sid 顺序渲染", list(t.extra_system) == keys,
          list(t.extra_system)[:4])

    # 3) 同槽多块保持追加序（followup 的 done/confirm/drop）
    t = mk()
    t.stage = RENDER_SLOTS
    t.set_slot("followup", "done", order=0)
    t.set_slot("followup", "confirm", order=1)
    t.set_slot("followup", "drop", order=2)
    check("同槽多块按 order 渲染",
          [x for x in t.extra_system if x in ("done", "confirm", "drop")]
          == ["done", "confirm", "drop"], t.slots.get("followup"))

    # 4) 未赋值的槽不出现
    t = mk()
    t.stage = RENDER_SLOTS
    t.set_slot("weather", "w")
    check("未赋值的槽不出现", list(t.extra_system) == ["w"], list(t.extra_system))

    # 5) set_slot(None) 不写
    t = mk()
    t.stage = RENDER_SLOTS
    t.set_slot("weather", None)
    check("set_slot(None) 不写空块", list(t.extra_system) == [])

    # 6) 只读：返回值是 tuple，且没有 append
    t = mk()
    t.stage = RENDER_SLOTS
    out = t.extra_system
    check("extra_system 是 tuple", isinstance(out, tuple), type(out).__name__)
    check("extra_system 没有 append", not hasattr(out, "append"))

    # 7) 九槽全亮：相邻关系全部被覆盖
    t = mk()
    t.stage = RENDER_SLOTS
    for k in keys:
        t.set_slot(k, "[" + k + "]")
    check("九槽全亮且顺序正确", list(t.extra_system) == ["[" + k + "]" for k in keys])

    # 8) sid 唯一（R2 的语义：唯一即可，不要求书写递增）
    sids = [s.sid for s in SLOTS]
    check("sid 唯一", len(sids) == len(set(sids)), sids)
    check("slot key 唯一", len(keys) == len(set(keys)))

    # 9) 契约层 mutate：把 followup 的三块反序，输出必须跟着变
    t1 = mk()
    t1.stage = RENDER_SLOTS
    for i, x in enumerate(("done", "confirm", "drop")):
        t1.set_slot("followup", x, order=i)
    t2 = mk()
    t2.stage = RENDER_SLOTS
    for x, o in (("drop", 2), ("confirm", 1), ("done", 0)):
        t2.set_slot("followup", x, order=o)
    check("调用序反转不影响输出（子顺序由 order 决定）",
          list(t1.extra_system) == list(t2.extra_system) == ["done", "confirm", "drop"],
          list(t2.extra_system))

    print("\n=== 槽位契约：%d/%d 通过 ===" % (PASS, PASS + FAIL))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())