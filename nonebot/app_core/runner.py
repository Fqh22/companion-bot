"""[orchestration] 通道无关的一轮对话：typing、流式、总时限、分段节奏。

为什么要这一层：现在这套「锁 → 取回合 → 调引擎 → 边生成边发 → 补发尾巴」
写在 plugins/chat 里，换通道就得重写 300 行。这里把它抽出来，通道只实现
Channel 的三个原语：

    send_text(session, text) -> bool     发一条文字，返回是否成功
    send_image(session, path) -> bool    发一张图
    typing(session, on) -> None          显示/关闭「正在输入」（不支持就空实现）

引擎只认 InboundMessage/OutboundMessage，所以新通道不需要 NoneBot、不需要 QQ。

会话锁：原设计说「留给入站侧的 TurnQueue」，但 TurnQueue 至今只有定义、没有使用点
（2026-11-15 确认），并发轮次会各自 prepare 并互相覆盖记忆/情绪/跟催状态。
所以锁放在这里——宁可违背这条注释，也不要留一个只存在于文档里的保障。
typing 心跳和 45 秒硬线也在这里。
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Protocol

from app_core import mood, pacing
from app_core.delivery import Capability, DeliveryPolicy
from app_core.engine import Engine, InboundMessage, OutboundMessage
from app_core.message import Part as _Part

log = logging.getLogger("app_core.runner")


class Channel(Protocol):
    """一个通道 = 一份能力声明 + 一个 send。

    forms 声明这个平台支持哪些形式（"text" / "image" / "voice" / "drawing" / "file"）。
    runner 只按 Part.form 调 send；平台不支持的形式由 runner 侧降级，
    所以加一个平台只需要写 adapter，不需要改业务代码。
    """

    forms: set[str]

    async def send_part(self, session: str, part) -> bool: ...

    async def typing(self, session: str, on: bool) -> None: ...

    async def typing(self, session: str, on: bool) -> None: ...


# 同一会话串行：两轮并发会各自 prepare，互相覆盖记忆/情绪/跟催状态。
# 并发消息在真人聊天里也是「一句一句回」，排队是更对的行为，不是延迟缺陷。
_SESSION_LOCKS: dict[str, asyncio.Lock] = {}


class TurnRunner:
    """跑完一轮：typing 心跳 + 引擎 + 流式发送 + 补发尾巴。"""

    def __init__(self, engine: Engine, channel: Channel,
                 caps: Capability | None = None,
                 policy: DeliveryPolicy | None = None,
                 deadline: float = 45.0,
                 typing_interval: float = 5.0) -> None:
        self.engine = engine
        self.channel = channel
        self.caps = caps
        self.policy = policy
        self.deadline = float(deadline)
        self.typing_interval = float(typing_interval)

    async def _send_part(self, session: str, part) -> bool:
        """按形式发送；平台不支持的形式在这里降级。

        降级放在 runner 而不是 adapter：adapter 只回答「我会什么」，
        「不会的时候退成什么」是编排层的决定，换平台不用重复写。
        """
        forms = getattr(self.channel, "forms", None) or {"text", "image"}
        if part.form in forms:
            try:
                return bool(await self.channel.send_part(session, part))
            except Exception as e:  # noqa: BLE001
                log.warning("[runner] 发送 %s 失败：%s", part.form, e)
                return False
        if part.form == "voice":
            # 语音发不了就说一句，不要静默丢——用户会以为漏回了
            return await self._send_part(session, _Part("text", "（这条本来是语音，这里发不了）"))
        log.info("[runner] 通道 %s 不支持 %s，已跳过", getattr(self.channel, "caps", "?"), part.form)
        return False

    async def _typing_loop(self, session: str) -> None:
        """心跳：第一次由 run() 立刻发，这里只负责续期。"""
        try:
            while True:
                await asyncio.sleep(self.typing_interval)
                await self.channel.typing(session, True)
        except asyncio.CancelledError:
            raise

    async def _pace_and_send(self, session: str, text: str, sent: int,
                             started: float) -> bool:
        """发出这一条之前等「打字」时间；预算见底就跳过等待。"""
        left = self.deadline - (time.monotonic() - started)
        if left <= 0:
            return False
        try:
            m = mood.settle(mood.load())
        except Exception:  # noqa: BLE001
            m = {}
        gap = pacing.gap_seconds(sent) if sent else 0.0
        pace = pacing.pace_seconds(text, datetime.now().hour, m,
                                   fast=pacing.is_fast(sent) or left < 12)
        wait = min(gap + pace, max(0.0, left - 4))    # 留 4 秒给发送和收尾
        if wait > 0:
            await asyncio.sleep(wait)
        return await self._send_part(session, _Part("text", text))

    async def run(self, msg: InboundMessage) -> OutboundMessage:
        """跑一轮。同一会话串行（见 _SESSION_LOCKS），超时抛 asyncio.TimeoutError。"""
        lock = _SESSION_LOCKS.setdefault(msg.session, asyncio.Lock())
        if lock.locked():
            log.info("[runner] 上一轮未结束，排队等待：%s", str(msg.session)[:24])
        async with lock:
            return await self._run(msg)

    async def _run(self, msg: InboundMessage) -> OutboundMessage:
        """真正的一轮：typing 心跳 + 引擎 + 流式发送 + 补发尾巴。"""
        started = time.monotonic()
        sent = 0
        # 立刻亮一次「正在输入」：等 task 调度会晚一个 tick，短回复可能整个错过
        try:
            await self.channel.typing(msg.session, True)
        except Exception:  # noqa: BLE001
            pass
        typing = asyncio.create_task(self._typing_loop(msg.session))

        async def on_sentence(part: str) -> bool:
            """返回**是否真的发出去了**——调用方要用它决定记不记 sent_parts。

            原来返回 None，chat_turn 无条件 append，发失败也当成了已发：
            后续「一个字都没发才重试」的判断会失效。
            """
            nonlocal sent
            if await self._pace_and_send(msg.session, part, sent, started):
                sent += 1
                return True
            return False

        try:
            out = await asyncio.wait_for(
                self.engine.handle(msg, on_sentence=on_sentence, caps=self.caps,
                                   delivery_policy=self.policy),
                timeout=self.deadline)
        finally:
            typing.cancel()
            try:
                await self.channel.typing(msg.session, False)
            except Exception:  # noqa: BLE001
                pass

        for i, part in enumerate(out.chunks):
            if await self._pace_and_send(msg.session, part, sent + i, started):
                sent += 1
        for path in out.images:
            try:
                await self._send_part(msg.session, _Part("image", path))
            except Exception:  # noqa: BLE001
                pass
        return out
