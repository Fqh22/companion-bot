"""通道无关层（app_core.runner）的离线测试。

不导入 nonebot、不碰 QQ：验证「换通道时真正要复用的那部分」——
流式发送顺序、typing 开关、补发尾巴、超时抛错。
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

CODE = Path(os.environ.get("QQBOT_CODE_DIR", "/opt/nonebot")).resolve()
sys.path.insert(0, str(CODE))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

from app_core import pacing  # noqa: E402
from app_core.engine import InboundMessage, OutboundMessage  # noqa: E402
from app_core.runner import TurnRunner  # noqa: E402

pacing.pace_seconds = lambda *a, **k: 0.0
pacing.gap_seconds = lambda *a, **k: 0.0

RESULTS: list[bool] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    RESULTS.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + name + (f" | {extra}" if extra else ""))


class FakeChannel:
    def __init__(self) -> None:
        self.texts: list[str] = []
        self.images: list[str] = []
        self.voices: list[str] = []
        self.typing_on = 0
        self.typing_off = 0

    forms = {"text", "image", "voice"}

    async def send_part(self, session: str, part) -> bool:
        """协议入口：runner 现在按 Part.form 调这个，不再直接调 send_text。"""
        if part.form == "text":
            return await self.send_text(session, part.content)
        if part.form == "image":
            return await self.send_image(session, part.content)
        if part.form == "voice":
            return await self.send_voice(session, part.content)
        return False

    async def send_text(self, session: str, text: str) -> bool:
        self.texts.append(text)
        return True

    async def send_image(self, session: str, path: str) -> bool:
        self.images.append(path)
        return True

    async def send_voice(self, session: str, path: str) -> bool:
        self.voices.append(path)
        return True

    async def typing(self, session: str, on: bool) -> None:
        if on:
            self.typing_on += 1
        else:
            self.typing_off += 1


class FakeEngine:
    def __init__(self, parts: list[str], out: OutboundMessage, delay: float = 0.0):
        self.parts = parts
        self.out = out
        self.delay = delay

    async def handle(self, msg, on_sentence=None, caps=None, delivery_policy=None):
        for p in self.parts:
            if on_sentence:
                await on_sentence(p)
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.out


async def main() -> None:
    # 1. 流式先发，尾巴补发，图片最后发
    ch = FakeChannel()
    eng = FakeEngine(["一句。", "两句。"], OutboundMessage(chunks=["尾巴。"], images=["x.png"]))
    await TurnRunner(eng, ch, deadline=5).run(InboundMessage(session="private:1", text="hi"))
    check("流式段按顺序发出", ch.texts[:2] == ["一句。", "两句。"], str(ch.texts))
    check("尾巴补发在最后", ch.texts[-1] == "尾巴。", str(ch.texts))
    check("图片也发了", ch.images == ["x.png"], str(ch.images))
    check("typing 开了也关了", ch.typing_on >= 1 and ch.typing_off >= 1,
          f"{ch.typing_on}/{ch.typing_off}")

    # 2. 超时抛 TimeoutError，且 typing 被关掉
    ch2 = FakeChannel()
    eng2 = FakeEngine([], OutboundMessage(), delay=1.0)
    timed_out = False
    try:
        await TurnRunner(eng2, ch2, deadline=0.2).run(
            InboundMessage(session="private:2", text="hi"))
    except asyncio.TimeoutError:
        timed_out = True
    check("超时抛 TimeoutError", timed_out)
    check("超时也会关 typing", ch2.typing_off >= 1, str(ch2.typing_off))

    # 3. 发送失败不崩：channel 返回 False 时继续
    class FailChannel(FakeChannel):
        async def send_text(self, session, text):
            return False
    ch3 = FailChannel()
    await TurnRunner(FakeEngine(["a。"], OutboundMessage(chunks=["b。"])),
                     ch3, deadline=5).run(InboundMessage(session="private:3", text="hi"))
    check("发送失败不抛异常", ch3.texts == [], str(ch3.texts))

    # 4. 语音：通道会发就照发；不支持 / 发送失败都降级成文字，不静默丢
    ch4 = FakeChannel()
    await TurnRunner(FakeEngine([], OutboundMessage(audio=["v.silk"])), ch4, deadline=5).run(
        InboundMessage(session="private:4", text="hi"))
    check("语音 Part 走 send_part", ch4.voices == ["v.silk"], str(ch4.voices))

    class NoVoiceChannel(FakeChannel):
        forms = {"text", "image"}
    ch5 = NoVoiceChannel()
    await TurnRunner(FakeEngine([], OutboundMessage(audio=["v.silk"])), ch5, deadline=5).run(
        InboundMessage(session="private:5", text="hi"))
    check("通道不支持语音时降级成文字",
          any("本来是语音" in t for t in ch5.texts), str(ch5.texts))

    class VoiceFailChannel(FakeChannel):
        async def send_voice(self, session, path):
            return False
    ch6 = VoiceFailChannel()
    await TurnRunner(FakeEngine([], OutboundMessage(audio=["v.silk"])), ch6, deadline=5).run(
        InboundMessage(session="private:6", text="hi"))
    check("语音发送失败也降级成文字",
          any("本来是语音" in t for t in ch6.texts), str(ch6.texts))

    # 5. 生成图：不支持 drawing 的通道退化成普通图片（docs/19 的降级规则）
    from app_core.message import Part as _P

    class NoDrawChannel(FakeChannel):
        forms = {"text", "image"}
    ch7 = NoDrawChannel()
    r7 = TurnRunner(FakeEngine([], OutboundMessage()), ch7, deadline=5)
    await r7._send_part("private:7", _P("drawing", "d.png"))
    check("不支持生成图的通道退化成图片", ch7.images == ["d.png"], str(ch7.images))

    passed = sum(1 for r in RESULTS if r)
    print(f"\n=== {passed}/{len(RESULTS)} 通过 ===")
    sys.exit(0 if passed == len(RESULTS) else 1)


if __name__ == "__main__":
    asyncio.run(main())
