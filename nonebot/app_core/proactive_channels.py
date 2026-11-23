"""[orchestration] 主动消息通道注册表：一个主体、多个出场通道。

以前主动消息只认 QQ：`plugins/proactive` 直接 `bot.call_api("send_private_msg")`，
飞书插件虽有 `_send_message`，却只在被动回复里用（会话映射 `_chat_of` 只写不读）。
这里放一个极薄的注册表，通道插件在 startup 时注册自己的发送函数，
调度侧按 `proactive.channel` 选一个；**默认 qq，现状不变**，
注册不等于启用，也不会双发。

不做广播、不做无人监督的多通道推送：一次只发一个通道，发不出去由调用方回退。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable

@dataclass(frozen=True)
class Receipt:
    platform: str
    message_id: str

    @property
    def reference(self) -> str:
        return f"{self.platform}:{self.message_id}" if self.message_id else ""


Sender = Callable[[str], Awaitable[bool | Receipt]]
_SENDERS: dict[str, Sender] = {}


def register(name: str, fn: Sender) -> None:
    _SENDERS[str(name).strip().lower()] = fn


def unregister(name: str) -> None:
    _SENDERS.pop(str(name).strip().lower(), None)


def available() -> list[str]:
    return sorted(_SENDERS)


async def send(text: str, channel: str) -> bool | Receipt:
    """发到指定通道；没注册 / 抛异常都返回 False，由调用方决定回退。"""
    fn = _SENDERS.get(str(channel or "").strip().lower())
    if fn is None:
        return False
    try:
        return await fn(text)
    except Exception:  # noqa: BLE001
        return False
