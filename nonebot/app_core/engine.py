"""发动机门面：QQ 插件和未来的其他入口都只经过这里。

这一层现在很薄：消息进、ChatCore.reply 出，目的是先把“入口 -> 引擎 -> 出口”
的边界立起来。后续 console 适配器、身份映射、投递策略都往这里长；
ChatCore 本身先不动，保证 QQ 行为不变。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app_core.delivery import Capability, DeliveryPolicy


@dataclass(frozen=True)
class InboundMessage:
    """归一化后的入站消息；现在先兼容 ChatCore 的 session 字符串。"""

    session: str
    text: str
    images: list[str] = field(default_factory=list)
    platform: str = "qq"
    external_user_id: str = ""
    external_chat_id: str | None = None
    event_id: str | None = None
    reply_to: str | None = None
    store_text: str | None = None


@dataclass
class OutboundMessage:
    """一轮回复的出口：文字分段 + 待发图片。"""

    chunks: list[str] = field(default_factory=list)
    images: list[str] = field(default_factory=list)


class Engine:
    """最小门面：先包住 ChatCore.reply，不搬任何业务代码。"""

    def __init__(self, core) -> None:
        self.core = core

    async def handle(self, msg: InboundMessage, on_sentence=None,
                     caps: Capability | None = None,
                     delivery_policy: DeliveryPolicy | None = None) -> OutboundMessage:
        kwargs = {}
        if caps is not None:
            kwargs["caps"] = caps
        if delivery_policy is not None:
            kwargs["delivery_policy"] = delivery_policy
        replies, images = await self.core.reply(
            msg.session, msg.text, list(msg.images) or None,
            on_sentence=on_sentence, store_text=msg.store_text, **kwargs)
        return OutboundMessage(chunks=list(replies or []), images=list(images or []))
