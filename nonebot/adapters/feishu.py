"""飞书入站归一化：把飞书事件的字段变成 InboundMessage。

不 import nonebot / lark：插件把事件里已经取好的字段传进来，便于离线测试。
飞书的 content 是一个 JSON 字符串，不同 msg_type 字段完全不同——解析放在这里，
既免得插件里到处 if，也让「文本/图片/语音」三种形式能单独测。
"""
from __future__ import annotations

import json
import re

from app_core.engine import InboundMessage

# 群聊里 @ 机器人会留下 @_user_1 之类的占位，私聊不会有，统一清掉
_RE_MENTION = re.compile(r"@_user_\d+")


def to_inbound(*, open_id: str, chat_id: str, text: str,
               images: list[str] | None = None,
               event_id: str | None = None,
               voice: bool = False,
               session: str | None = None,
               store_text: str | None = None) -> InboundMessage:
    """飞书私聊事件 → 内核入站消息。

    session 由调用方决定：主人走 config.owner_session()（和 QQ 共用一份记忆），
    其他人（当前不允许）用 feishu:<open_id>。
    """
    uid = str(open_id or "").strip()
    cid = str(chat_id or "").strip()
    if session is None:
        session = f"feishu:{uid}" if uid else "feishu:unknown"
    return InboundMessage(
        session=session, text=text or "", images=list(images or []),
        platform="feishu", external_user_id=uid,
        external_chat_id=cid or None, event_id=event_id, voice=voice,
        store_text=store_text)


def _post_text(obj: dict) -> tuple[str, list[str]]:
    """富文本 post 消息：拼出纯文本 + 其中内嵌的图片 key。"""
    texts: list[str] = []
    images: list[str] = []
    title = str(obj.get("title") or "").strip()
    if title:
        texts.append(title)
    for line in obj.get("content") or []:
        for node in line or []:
            if not isinstance(node, dict):
                continue
            tag = node.get("tag")
            if tag == "text":
                texts.append(str(node.get("text") or ""))
            elif tag == "a":
                texts.append(str(node.get("text") or ""))
            elif tag == "img":
                key = str(node.get("image_key") or "")
                if key:
                    images.append(key)
    return "\n".join(t for t in texts if t).strip(), images


def parse_content(message_type: str, content: str) -> dict:
    """飞书 content(JSON 字符串) → {text, image_keys, audio_key, unsupported}。

    - text: {"text": "..."}
    - image: {"image_key": "img_..."}
    - audio: {"file_key": "file_...", "duration": 1000}
    - file: {"file_key": "...", "file_name": "..."}
    - post: 富文本（标题 + 段落 + 内嵌图片）
    其他类型原样返回 unsupported=message_type，由插件决定怎么回。
    """
    try:
        obj = json.loads(content or "{}")
    except Exception:  # noqa: BLE001
        obj = {}
    if not isinstance(obj, dict):
        obj = {}
    text, images, audio, unsupported = "", [], "", ""
    if message_type == "text":
        text = _RE_MENTION.sub("", str(obj.get("text") or "")).strip()
    elif message_type == "image":
        key = str(obj.get("image_key") or "")
        if key:
            images.append(key)
    elif message_type in ("audio", "media"):
        audio = str(obj.get("file_key") or "")
    elif message_type == "post":
        text, images = _post_text(obj)
    elif message_type == "file":
        unsupported = "file"
    else:
        unsupported = message_type
    return {"text": text, "image_keys": images, "audio_key": audio,
            "unsupported": unsupported}
