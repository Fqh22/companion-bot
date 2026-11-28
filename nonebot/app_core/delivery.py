"""[domain] 投递策略：平台能力（硬限制）和说话风格（软策略）分开。

QQ 现有行为：动态段长 18–32，最多 6 段；这些是风格，不是平台上限。
真正的平台上限（字节数、是否支持图片/按钮/编辑）放 Capability。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app_core.prompt import split_reply

# 通道收下了文本，但还没投递给用户（语音先缓冲，等合成后再发）。
# 真值：调用方不要再发一遍。不是 True：历史要等真正送达再写。
BUFFERED = "buffered"


class UnknownDelivery:
    """传输中断，平台可能已经收下。"""

    def __bool__(self) -> bool:
        return False


DELIVERY_UNKNOWN = UnknownDelivery()


def is_buffered(result) -> bool:
    return result == BUFFERED


def is_delivered(result) -> bool:
    """真正交给平台了。缓冲不算。"""
    return result is True or isinstance(result, Receipt)


@dataclass(frozen=True)
class Receipt:
    """平台已接受消息。部分通道没有 ID，但仍可确认发送。"""

    platform: str
    message_id: str
    text: str | None = None

    @property
    def reference(self) -> str:
        return f"{self.platform}:{self.message_id}" if self.message_id else ""


DeliveryResult = bool | str | Receipt | UnknownDelivery


@dataclass
class ConfirmedText:
    """本批真正送达的文字及平台引用，供历史账本一次写入。"""

    parts: list[str] = field(default_factory=list)
    references: list[tuple[str, str]] = field(default_factory=list)

    def add(self, text: str, result: DeliveryResult) -> bool:
        if not is_delivered(result):
            return False
        if isinstance(result, Receipt):
            if result.text is not None:
                text = result.text
            if result.reference:
                self.references.append((result.reference, text))
        self.parts.append(text)
        return True


@dataclass(frozen=True)
class Capability:
    name: str = "qq"
    max_text_chars: int = 0          # 0 = 不设硬上限
    max_parts: int = 6
    supports_images: bool = True
    supports_markup: bool = False
    supports_buttons: bool = False
    supports_edit: bool = False
    supports_streaming: bool = True


@dataclass(frozen=True)
class DeliveryPolicy:
    # 风格参数：不是平台限制；console 可以沿用，其他平台可按需改
    max_chars: int = 25
    max_parts: int = 6
    gap_ms: int = 0
    reply_deadline: float = 45.0
    streaming: bool = True


def plan_delivery(text: str, policy: DeliveryPolicy | None = None,
                  caps: Capability | None = None) -> list[str]:
    """按策略 + 平台硬限制切段；默认输出与当前 QQ split_reply 一致。"""
    p = policy or DeliveryPolicy()
    c = caps or Capability()
    limit = max(1, int(p.max_chars))
    if c.max_text_chars > 0:
        limit = min(limit, max(1, int(c.max_text_chars)))
    parts = min(max(1, int(p.max_parts)), max(1, int(c.max_parts)))
    out = split_reply(text or "", limit, parts)
    # split_reply 的 max_len 是软约束；平台硬上限要真截断，避免超长消息被拒。
    if c.max_text_chars > 0:
        hard = max(1, int(c.max_text_chars))
        out = [piece for part in out
               for piece in (part[i:i + hard] for i in range(0, len(part), hard))]
    return out
