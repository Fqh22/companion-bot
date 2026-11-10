"""投递策略：平台能力（硬限制）和说话风格（软策略）分开。

QQ 现有行为：动态段长 18–32，最多 6 段；这些是风格，不是平台上限。
真正的平台上限（字节数、是否支持图片/按钮/编辑）放 Capability。
"""
from __future__ import annotations

from dataclasses import dataclass

from app_core.prompt import split_reply


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
