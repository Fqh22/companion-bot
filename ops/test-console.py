#!/usr/bin/env python3
"""适配器冒烟：FakeChannel 记录、ConsoleChannel 输出。"""
from __future__ import annotations

import asyncio
import io
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CODE = HERE.parent / "nonebot"
if not (CODE / "app_core").is_dir():
    CODE = HERE.parent
sys.path.insert(0, str(CODE))
sys.path.insert(0, str(HERE.parent))

from adapters.console import ConsoleChannel  # noqa: E402
from adapters.fake import FakeChannel  # noqa: E402
from app_core.engine import Engine, InboundMessage, OutboundMessage  # noqa: E402

PASS = FAIL = 0


def check(name, ok, extra=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("PASS " + name)
    else:
        FAIL += 1
        print("FAIL " + name + (" | " + str(extra) if extra else ""))


class FakeCore:
    async def reply(self, session, text, images=None, on_sentence=None,
                    store_text=None):
        return ["收到", "第二句"], ["img.png"]


class FakeEngine:
    async def handle(self, msg, on_sentence=None):
        return OutboundMessage(chunks=["假回复"], images=[])


async def main():
    ch = FakeChannel()
    await ch.send(OutboundMessage(chunks=["a", "b"], images=["x.png"]))
    check("FakeChannel 记录一条", len(ch.sent) == 1)
    check("FakeChannel 内容完整", ch.sent[0].chunks == ["a", "b"]
          and ch.sent[0].images == ["x.png"])

    out = await Engine(FakeCore()).handle(InboundMessage(session="s", text="hi"))
    await ch.send(out)
    check("Engine -> Channel 文本", ch.sent[-1].chunks == ["收到", "第二句"])
    check("Engine -> Channel 图片", ch.sent[-1].images == ["img.png"])

    check("ConsoleChannel 有 console capability",
          ConsoleChannel(io.StringIO()).caps.name == "console")

    buf = io.StringIO()
    await ConsoleChannel(buf).send(OutboundMessage(chunks=["在"], images=["a.jpg"]))
    text = buf.getvalue()
    check("ConsoleChannel 打印文本", "在" in text, text)
    check("ConsoleChannel 打印图片", "[图片] a.jpg" in text, text)

    # run() 会阻塞在 input()，不在这里测；只测 send() 的边界。


if __name__ == "__main__":
    asyncio.run(main())
    print(f"\n=== {PASS}/{PASS + FAIL} 通过 ===")
    raise SystemExit(0 if FAIL == 0 else 1)
