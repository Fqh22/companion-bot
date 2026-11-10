"""[orchestration] 命令分发：平台无关的「/xxx 参数」解析与调用。

插件层（NoneBot）和新通道（console 等）共用同一个注册表：
命令清单仍然只写在 `help.py` 一处，这里只负责「解析 → 找 handler → 调」。

handler 是异步函数，收 `CommandContext`，返回要发回去的文本。
有副作用的命令（清理、恢复、审批）暂时没搬过来，新通道遇到就回「这个通道暂不支持」。
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Awaitable, Callable


@dataclass(frozen=True)
class CommandContext:
    session: str
    user_id: str = ""
    is_owner: bool = False
    args: str = ""


@dataclass(frozen=True)
class CommandSpec:
    name: str
    description: str
    handler: Callable[[CommandContext], Awaitable[str]]
    owner_only: bool = True


class CommandRegistry:
    def __init__(self) -> None:
        self._specs: dict[str, CommandSpec] = {}

    def register(self, spec: CommandSpec) -> None:
        self._specs[spec.name.strip().lower()] = spec

    def names(self) -> list[str]:
        return sorted(self._specs)

    @staticmethod
    def parse(text: str) -> tuple[str, str] | None:
        """`/mood 7` → (`mood`, `7`)；不是命令返回 None。"""
        t = (text or "").strip()
        if not t.startswith("/"):
            return None
        head, _, rest = t[1:].partition(" ")
        name = head.strip().lower()
        if not name:
            return None
        return name, rest.strip()

    async def dispatch(self, text: str, ctx: CommandContext) -> str | None:
        """返回 None 表示「不是命令」；返回 str 表示要发回去的内容。"""
        parsed = self.parse(text)
        if parsed is None:
            return None
        name, args = parsed
        spec = self._specs.get(name)
        if spec is None:
            return None
        if spec.owner_only and not ctx.is_owner:
            return "这条命令只有主人能用。"
        return await spec.handler(replace(ctx, args=args))


def default_registry(core) -> CommandRegistry:
    """新通道的默认命令集：全部是只读命令，复用 introspect / help。

    有副作用的命令（清理、恢复、审批、写日记）暂时留在 NoneBot 插件里；
    新通道遇到会返回 None，由调用方决定「不支持」还是转人工。
    """
    from app_core import help as help_mod
    from app_core import introspect, learn

    async def _ping(ctx: CommandContext) -> str:
        return introspect.ping_text(core)

    async def _help(ctx: CommandContext) -> str:
        return help_mod.text()

    async def _debug(ctx: CommandContext) -> str:
        return help_mod.debug_text()

    async def _mood(ctx: CommandContext) -> str:
        return introspect.mood_text()

    async def _usage(ctx: CommandContext) -> str:
        return await introspect.usage_text(core)

    async def _mem_status(ctx: CommandContext) -> str:
        return introspect.mem_status_text()

    async def _profile(ctx: CommandContext) -> str:
        return introspect.profile_text(learn)

    r = CommandRegistry()
    for name, desc, fn in (
        ("ping", "测试 + 配置状态", _ping),
        ("help", "命令清单", _help),
        ("debug", "维护命令清单", _debug),
        ("mood", "心情 / 精力 / 互动", _mood),
        ("usage", "token 与费用", _usage),
        ("mem_status", "记忆链路快照", _mem_status),
        ("profile", "它记住的你", _profile),
    ):
        r.register(CommandSpec(name, desc, fn))
    return r
