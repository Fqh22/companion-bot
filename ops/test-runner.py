"""通道无关层（app_core.runner）的离线测试。

不导入 nonebot、不碰 QQ：验证「换通道时真正要复用的那部分」——
流式发送顺序、typing 开关、补发尾巴、超时抛错。
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
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

from harness import check, report  # noqa: E402


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

    # 5. 生成图：不支持 drawing 的通道退化成普通图片（docs/02-系统与扩展.md 的降级规则）
    from app_core.message import Part as _P

    class NoDrawChannel(FakeChannel):
        forms = {"text", "image"}
    ch7 = NoDrawChannel()
    r7 = TurnRunner(FakeEngine([], OutboundMessage()), ch7, deadline=5)
    await r7._send_part("private:7", _P("drawing", "d.png"))
    check("不支持生成图的通道退化成图片", ch7.images == ["d.png"], str(ch7.images))

    # 6. 无进展超时（批次 2）：首句发出后卡住，按 stall 收尾，而不是等总上限
    class StallEngine(FakeEngine):
        async def handle(self, msg, on_sentence=None, caps=None, delivery_policy=None):
            if on_sentence:
                await on_sentence("第一句。")
            await asyncio.sleep(5)
            return self.out

    ch8 = FakeChannel()
    t0 = time.monotonic()
    stalled = False
    try:
        await TurnRunner(StallEngine([], OutboundMessage()), ch8,
                         deadline=5, stall_seconds=0.2).run(
            InboundMessage(session="private:8", text="hi"))
    except asyncio.TimeoutError:
        stalled = True
    check("无进展超时会收尾并抛 TimeoutError", stalled)
    check("收尾前已发出的句子保留", ch8.texts == ["第一句。"], str(ch8.texts))
    check("无进展超时不会等到总上限", time.monotonic() - t0 < 2,
          f"{time.monotonic() - t0:.2f}s")

    # 7. 取证 / 首字阶段不受 stall 约束：慢一点但不发句子，仍应正常完成
    ch9 = FakeChannel()
    ok9 = True
    try:
        await TurnRunner(FakeEngine([], OutboundMessage(), delay=0.5), ch9,
                         deadline=5, stall_seconds=0.2).run(
            InboundMessage(session="private:9", text="hi"))
    except asyncio.TimeoutError:
        ok9 = False
    check("首字阶段不被无进展超时误杀", ok9)

    # 8. 默认值放宽：总上限只兜底，另有独立的无进展超时
    r_def = TurnRunner(FakeEngine([], OutboundMessage()), FakeChannel())
    check("总上限默认放宽（不再是 45）", r_def.deadline >= 60, str(r_def.deadline))
    check("默认带无进展超时", r_def.stall_seconds > 0, str(r_def.stall_seconds))

    return report()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
