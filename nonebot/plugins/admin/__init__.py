"""admin 插件：基础命令。"""
from __future__ import annotations

from nonebot import on_command
from nonebot.adapters.onebot.v11 import MessageEvent
from nonebot.permission import SUPERUSER

from app_core import help as help_mod
from app_core import introspect
from app_core import learn
from plugins.chat import core

ping = on_command("ping", priority=5, block=True, permission=SUPERUSER)
help_cmd = on_command("help", priority=5, block=True, permission=SUPERUSER)
debug_cmd = on_command("debug", priority=5, block=True, permission=SUPERUSER)
reload_cmd = on_command("reload_persona", priority=5, block=True, permission=SUPERUSER)
profile_cmd = on_command("profile", priority=5, block=True, permission=SUPERUSER)
forget_cmd = on_command("profile_forget", priority=5, block=True, permission=SUPERUSER)
clear_cmd = on_command("profile_clear", priority=5, block=True, permission=SUPERUSER)


def _arg(event: MessageEvent, cmd: str) -> str:
    """命令后面那一截参数。

    坑：`on_command` **不会**把命令本身从 `event.get_plaintext()` 里剥掉
    （`plugins/commands` 的 `_arg()` 存在就是为了剥它）。之前这里用
    `nonebot.get_current_event()` 取事件、又不剥前缀，两个问题叠在一起：
    取不到事件时静默返回空串，取到了也会把「/profile_forget 家庭」整体当字段名，
    于是这条命令永远只会回「用法：…」或「没有这个字段」。参数一律从 handler 的
    event 拿，别去猜当前事件。
    """
    return (event.get_plaintext() or "").replace(cmd, "", 1).strip()


@ping.handle()
async def _ping():
    """pong + 一行配置状态。

    配上意义：`admin.user_id` 没填时记忆不写、主动消息不发，但服务看着一切正常。
    /ping 是唯一能一眼看出「它其实是个哑巴」的地方，所以状态挂在这里。
    """
    await ping.finish(introspect.ping_text(core))


@help_cmd.handle()
async def _help():
    """/help 由 app_core/help.py 渲染：命令只写一处，lint 保证不漂。"""
    await help_cmd.finish(help_mod.text())


@debug_cmd.handle()
async def _debug():
    """/debug：维护与排障命令全量。日常清单里只留了 11 条。"""
    await debug_cmd.finish(help_mod.debug_text())


@reload_cmd.handle()
async def _reload(event: MessageEvent):
    core.reload_persona()
    await reload_cmd.finish(f"人格已重载（{len(core.persona.get('system_prompt', ''))} 字）")


@profile_cmd.handle()
async def _profile():
    """看当前画像：每个字段的置信度、多久没被确认、会不会被注入。"""
    await profile_cmd.finish(introspect.profile_text(learn))


@forget_cmd.handle()
async def _forget(event: MessageEvent):
    from app_core import profile as profile_mod

    arg = _arg(event, "/profile_forget")
    if not arg:
        await forget_cmd.finish("用法：/profile_forget 职业与求职")
    if not profile_mod.forget(learn.profile_path(), arg):
        keys = "、".join(learn.profile_fields().keys()) or "（空）"
        await forget_cmd.finish(f"没有这个字段。现有：{keys}")
    await forget_cmd.finish(f"已删掉「{arg}」，下次复盘不会自动加回来（除非又有新证据）")


@clear_cmd.handle()
async def _clear():
    from app_core import profile as profile_mod

    profile_mod.clear(learn.profile_path())
    await clear_cmd.finish("画像已清空。它会从下次复盘开始重新长，只记那时候看到的事。")
