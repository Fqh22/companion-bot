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


class FakeCore:
    """只记历史，验证「通道确认了才写」。"""

    def __init__(self) -> None:
        self.lines: list[str] = []

    async def remember_safe(self, session: str, role: str, text: str) -> int:
        self.lines.append(text)
        return 1


class FakeEngine:
    def __init__(self, parts: list[str], out: OutboundMessage, delay: float = 0.0,
                 core: FakeCore | None = None):
        self.parts = parts
        self.out = out
        self.delay = delay
        self.core = core

    async def handle(self, msg, on_sentence=None, caps=None, delivery_policy=None,
                     defer_history=False):
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
    # deadline 在 runner 里被夹到最少 1.0s；延迟必须长于它，否则是竞态。
    eng2 = FakeEngine([], OutboundMessage(), delay=2.0)
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
        async def handle(self, msg, on_sentence=None, caps=None, delivery_policy=None,
                         defer_history=False):
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

    # 9. 历史只写通道确认过的段：首段成功、尾段失败 → 只记首段
    class FailTailChannel(FakeChannel):
        async def send_text(self, session, text):
            if text.startswith("首段"):
                self.texts.append(text)
                return True
            return False

    core9 = FakeCore()
    eng9 = FakeEngine(["首段。"], OutboundMessage(chunks=["尾段。"]), core=core9)
    await TurnRunner(eng9, FailTailChannel(), deadline=5).run(
        InboundMessage(session="private:9", text="hi"))
    check("首段成功尾段失败 → 历史只有首段", core9.lines == ["首段。"], str(core9.lines))

    core10 = FakeCore()
    await TurnRunner(FakeEngine(["全部。"], OutboundMessage(), core=core10),
                     FailChannel(), deadline=5).run(
        InboundMessage(session="private:10", text="hi"))
    check("全部发送失败 → 一条历史都不写", core10.lines == [], str(core10.lines))

    # 9b. 图片投递结果要回填：失败不能算已发
    class NoImageChannel(FakeChannel):
        async def send_image(self, session, path):
            return False

    out_bad = OutboundMessage(images=["bad.png"])
    await TurnRunner(FakeEngine([], out_bad), NoImageChannel(), deadline=5).run(
        InboundMessage(session="private:img1", text="hi"))
    check("图片发送失败 → images_sent 为空", out_bad.images_sent == [],
          str(out_bad.images_sent))
    out_ok = OutboundMessage(images=["ok.png"])
    await TurnRunner(FakeEngine([], out_ok), FakeChannel(), deadline=5).run(
        InboundMessage(session="private:img2", text="hi"))
    check("图片发送成功 → images_sent 记录", out_ok.images_sent == ["ok.png"],
          str(out_ok.images_sent))

    # 10. 平台发送挂死 → 到时限收尾，且会话锁释放，下一轮能继续
    class HangChannel(FakeChannel):
        async def send_text(self, session, text):
            await asyncio.sleep(30)
            return True

    t0 = time.monotonic()
    await TurnRunner(FakeEngine(["挂住。"], OutboundMessage(chunks=[])),
                     HangChannel(), deadline=0.5).run(
        InboundMessage(session="private:11", text="hi"))
    check("平台发送挂死时本轮能收尾", time.monotonic() - t0 < 3,
          f"{time.monotonic() - t0:.2f}s")
    ch11 = FakeChannel()
    await asyncio.wait_for(
        TurnRunner(FakeEngine(["下一轮。"], OutboundMessage()), ch11, deadline=5).run(
            InboundMessage(session="private:11", text="hi")), timeout=3)
    check("挂死之后同会话下一轮能继续", ch11.texts == ["下一轮。"], str(ch11.texts))

    from app_core.delivery import BUFFERED, Receipt

    class DeliveryCore:
        records = []

        async def record_delivery(self, session, text, *, references):
            self.records.append((session, text, references))

    class ReceiptChannel(FakeChannel):
        async def send_part(self, session, part):
            if part.content == "buffered":
                return BUFFERED
            if part.content == "failed":
                return False
            return Receipt("feishu", "test-message-id", "实际送达")

    ledger = DeliveryCore()
    await TurnRunner(FakeEngine(["buffered", "failed", "delivered"], OutboundMessage(),
                               core=ledger), ReceiptChannel(), deadline=5).run(
        InboundMessage(session="private:receipt", text="hi"))
    check("只记录送达正文与平台引用",
          ledger.records == [("private:receipt", "实际送达",
                              (("feishu:test-message-id", "实际送达"),))],
          ledger.records)

    return report()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
