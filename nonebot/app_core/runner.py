"""通道无关的一轮对话：typing、流式、总时限、分段节奏。

为什么要这一层：现在这套「锁 → 取回合 → 调引擎 → 边生成边发 → 补发尾巴」
写在 plugins/chat 里，换通道就得重写 300 行。这里把它抽出来，通道只实现
Channel 的三个原语：

    send_text(session, text) -> bool     发一条文字，返回是否成功
    send_image(session, path) -> bool    发一张图
    typing(session, on) -> None          显示/关闭「正在输入」（不支持就空实现）

引擎只认 InboundMessage/OutboundMessage，所以新通道不需要 NoneBot、不需要 QQ。

注意：这一层**不做**消息合并/会话锁——那是「入站侧」的事（TurnQueue），
和通道无关但需要事件循环，留给入口实现。typing 心跳和 45 秒硬线在这里。
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import Protocol

from app_core import mood, pacing
from app_core.delivery import Capability, DeliveryPolicy
from app_core.engine import Engine, InboundMessage, OutboundMessage


class Channel(Protocol):
    """一个通道只需要会三件事。"""

    async def send_text(self, session: str, text: str) -> bool: ...

    async def send_image(self, session: str, path: str) -> bool: ...

    async def typing(self, session: str, on: bool) -> None: ...


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
        return await self.channel.send_text(session, text)

    async def run(self, msg: InboundMessage) -> OutboundMessage:
        """跑一轮。超时抛 asyncio.TimeoutError，由调用方决定怎么兜底。"""
        started = time.monotonic()
        sent = 0
        # 立刻亮一次「正在输入」：等 task 调度会晚一个 tick，短回复可能整个错过
        try:
            await self.channel.typing(msg.session, True)
        except Exception:  # noqa: BLE001
            pass
        typing = asyncio.create_task(self._typing_loop(msg.session))

        async def on_sentence(part: str) -> None:
            nonlocal sent
            if await self._pace_and_send(msg.session, part, sent, started):
                sent += 1

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
                await self.channel.send_image(msg.session, path)
            except Exception:  # noqa: BLE001
                pass
        return out
