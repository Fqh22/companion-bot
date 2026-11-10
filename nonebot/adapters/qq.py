"""QQ/OneBot 入站归一化：把平台字段变成 InboundMessage。

不 import nonebot：插件拿到 event 后把字段传进来，便于离线测。
"""
from __future__ import annotations

from app_core.engine import InboundMessage


def to_inbound(*, user_id: str, text: str, chat_id: str | None = None,
               is_group: bool = False, images: list[str] | None = None,
               event_id: str | None = None, reply_to: str | None = None,
               store_text: str | None = None,
               session: str | None = None) -> InboundMessage:
    uid = str(user_id or "").strip()
    cid = str(chat_id).strip() if chat_id is not None else None
    if session is None:
        if is_group:
            session = f"group:{cid or 'unknown'}"
        else:
            session = f"private:{uid}"
    return InboundMessage(
        session=session, text=text or "", images=list(images or []),
        platform="qq", external_user_id=uid,
        external_chat_id=cid, event_id=event_id,
        reply_to=reply_to, store_text=store_text)
