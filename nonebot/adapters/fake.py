"""FakeChannel：测试用，只记录引擎发出来的东西。"""
from __future__ import annotations

from dataclasses import dataclass, field

from app_core.engine import OutboundMessage


@dataclass
class FakeChannel:
    """测试通道：认识所有形式，只记录不发。"""

    sent: list[OutboundMessage] = field(default_factory=list)
    parts: list = field(default_factory=list)      # 按 Part 记录的发送流水

    # 能力声明：测试通道全认识，降级逻辑由 runner 负责
    forms = {"text", "image", "voice", "drawing", "file"}

    async def send_part(self, session: str, part) -> bool:
        self.parts.append(part)
        return True

    async def send(self, out: OutboundMessage) -> None:
        self.sent.append(out)
