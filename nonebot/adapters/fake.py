"""FakeChannel：测试用，只记录引擎发出来的东西。"""
from __future__ import annotations

from dataclasses import dataclass, field

from app_core.engine import OutboundMessage


@dataclass
class FakeChannel:
    sent: list[OutboundMessage] = field(default_factory=list)

    async def send(self, out: OutboundMessage) -> None:
        self.sent.append(out)
