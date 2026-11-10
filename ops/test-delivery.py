#!/usr/bin/env python3
"""投递 golden：默认策略必须与当前 QQ 分段一致。"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CODE = HERE.parent / "nonebot"
if not (CODE / "app_core").is_dir():
    CODE = HERE.parent
sys.path.insert(0, str(CODE))

from app_core.delivery import Capability, DeliveryPolicy, plan_delivery  # noqa: E402

PASS = FAIL = 0

CASES = [
    ("你好。", ["你好。"]),
    ("今天天气不错。你要出去吗？我觉得可以。",
     ["今天天气不错。你要出去吗？", "我觉得可以。"]),
    ("第一句。第二句！第三句？第四句。第五句。第六句。第七句。",
     ["第一句。第二句！", "第三句？", "第四句。第五句。第六句。第七句。"]),
    ("这是一段没有句号的很长很长的文字" * 4,
     ["这是一段没有句号的很长很长的文字" * 4]),
    ("嗯", ["嗯"]),
    ("哈哈，这也行。", ["哈哈，这也行。"]),
    ("第一行\n第二行。第三行", ["第一行\n第二行。第三行"]),
    ("短。中中中中中中中中中中。长长长长长长长长长长长长长长长长长长长长长长长长长长长长。",
     ["短。中中中中中中中中中中。长长长长长长长长长长长长长长长长长长长长长长长长长长长长。"]),
    ("a. b. c. d. e. f. g. h.", ["a. b. c. d. e. f. g. h."]),
    ("最后一句话没有标点但是很长很长很长很长很长很长很长很长很长很长很长",
     ["最后一句话没有标点但是很长很长很长很长很长很长很长很长很长很长很长"]),
]


def check(name, ok, extra=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("PASS " + name)
    else:
        FAIL += 1
        print("FAIL " + name + (" | " + str(extra) if extra else ""))


def main():
    for i, (text, expected) in enumerate(CASES, 1):
        got = plan_delivery(text)
        check(f"golden {i}: 分段一致", got == expected, got)
        check(f"golden {i}: 不丢内容", "".join(got) == text, "".join(got))

    qq = Capability(name="qq")
    bad = [(t, plan_delivery(t, caps=qq), e) for t, e in CASES
           if plan_delivery(t, caps=qq) != e]
    check("QQ capability 不改变分段", not bad, bad[:1])

    cap = Capability(max_parts=2)
    got = plan_delivery(CASES[2][0], caps=cap)
    check("平台 max_parts 生效", len(got) <= 2, got)

    got = plan_delivery("a。b。c。d。", policy=DeliveryPolicy(max_chars=4))
    check("policy max_chars 生效", all(len(x) <= 4 for x in got), got)

    cap2 = Capability(max_text_chars=5)
    got = plan_delivery("a。b。c。d。", caps=cap2)
    check("capability max_text_chars 生效", all(len(x) <= 5 for x in got), got)

    check("空文本返回空", plan_delivery("") == [])

    print(f"\n=== {PASS}/{PASS + FAIL} 通过 ===")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
