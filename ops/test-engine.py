#!/usr/bin/env python3
"""Engine 门面：入口/出口边界的最小测试。"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CODE = HERE.parent / "nonebot"
if not (CODE / "app_core").is_dir():
    CODE = HERE.parent
sys.path.insert(0, str(CODE))

from app_core.engine import Engine, InboundMessage  # noqa: E402

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
    def __init__(self):
        self.calls = []

    async def reply(self, session, text, images=None, on_sentence=None,
                    store_text=None):
        self.calls.append((session, text, images, store_text))
        if on_sentence:
            await on_sentence("第一句")
        return ["第一句", "第二句"], ["img.png"]


async def main():
    core = FakeCore()
    seen = []

    async def cb(part):
        seen.append(part)

    out = await Engine(core).handle(
        InboundMessage(session="private:1", text="在吗",
                       images=["a.jpg"], store_text="入库版"),
        on_sentence=cb)
    check("返回 chunks", out.chunks == ["第一句", "第二句"], out.chunks)
    check("返回 images", out.images == ["img.png"], out.images)
    check("流式回调透传", seen == ["第一句"], seen)
    check("参数透传", core.calls == [("private:1", "在吗", ["a.jpg"], "入库版")],
          core.calls)

    core2 = FakeCore()
    out2 = await Engine(core2).handle(InboundMessage(session="s", text="x"))
    check("空图片传 None", core2.calls[0][2] is None, core2.calls)
    check("空回复归一成 list", out2.chunks == ["第一句", "第二句"], out2.chunks)


if __name__ == "__main__":
    asyncio.run(main())
    print(f"\n=== {PASS}/{PASS + FAIL} 通过 ===")
    raise SystemExit(0 if FAIL == 0 else 1)
