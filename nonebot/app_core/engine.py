"""[orchestration] 发动机门面：QQ 插件和未来的其他入口都只经过这里。

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
    files: list[str] = field(default_factory=list)
    audio: list[str] = field(default_factory=list)
    platform: str = "qq"
    external_user_id: str = ""
    external_chat_id: str | None = None
    event_id: str | None = None
    reply_to: str | None = None
    store_text: str | None = None
    voice: bool = False        # 这条文字是语音转写来的（形式进数据，不进正文）

    @property
    def parts(self) -> list:
        """形式视图：把旧字段翻译成 Part，不改存储。

        2026-11-15：形式统一到 Part 之后，加「语音/画画」不再需要新字段——
        入站语音走 audio、出站画图走 images（画出来的图就是一张图）。
        """
        from app_core.message import Part

        out = []
        if (self.text or "").strip():
            # 语音转写：形式是 voice，meta 里带 transcript 标记，
            # 需要人读的正文仍可从 content 取（docs/02-系统与扩展.md 的约定）
            out.append(Part("voice" if self.voice else "text", self.text,
                            {"transcript": True} if self.voice else {}))
        out += [Part("image", str(p)) for p in (self.images or [])]
        out += [Part("voice", str(p)) for p in (self.audio or [])]
        out += [Part("file", str(p)) for p in (self.files or [])]
        return out


@dataclass
class OutboundMessage:
    """一轮回复的出口：文字分段 + 待发图片 + 待发语音。"""

    chunks: list[str] = field(default_factory=list)
    images: list[str] = field(default_factory=list)
    audio: list[str] = field(default_factory=list)
    # 实际投递成功的图片/语音：runner 逐项发送后回填。插件不能只看 images
    # 非空就记「发了一张图」——发送失败时那是假的。
    images_sent: list[str] = field(default_factory=list)
    audio_sent: list[str] = field(default_factory=list)

    @property
    def parts(self) -> list:
        """形式视图：文字分段 → text Part，图片/语音各自成形。"""
        from app_core.message import Part

        out = [Part("text", str(c)) for c in (self.chunks or []) if str(c or "").strip()]
        out += [Part("image", str(p)) for p in (self.images or [])]
        out += [Part("voice", str(p)) for p in (self.audio or [])]
        return out


class Engine:
    """最小门面：先包住 ChatCore.reply，不搬任何业务代码。"""

    def __init__(self, core) -> None:
        self.core = core

    async def handle(self, msg: InboundMessage, on_sentence=None,
                     caps: Capability | None = None,
                     delivery_policy: DeliveryPolicy | None = None,
                     defer_history: bool = False) -> OutboundMessage:
        kwargs = {}
        if caps is not None:
            kwargs["caps"] = caps
        if delivery_policy is not None:
            kwargs["delivery_policy"] = delivery_policy
        replies, images = await self.core.reply(
            msg.session, msg.text, list(msg.images) or None,
            on_sentence=on_sentence, store_text=msg.store_text,
            defer_history=defer_history, **kwargs)
        return OutboundMessage(chunks=list(replies or []), images=list(images or []))
