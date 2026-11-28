"""admin 插件：基础命令。"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime
from pathlib import Path

from nonebot import on_command
from nonebot.adapters.onebot.v11 import MessageEvent, MessageSegment
from nonebot.permission import SUPERUSER

from companion_core import shared_application

core = shared_application().core

ping = on_command("ping", priority=5, block=True, permission=SUPERUSER)
help_cmd = on_command("help", priority=5, block=True, permission=SUPERUSER)
debug_cmd = on_command("debug", priority=5, block=True, permission=SUPERUSER)
reload_cmd = on_command("reload_persona", priority=5, block=True, permission=SUPERUSER)
profile_cmd = on_command("profile", priority=5, block=True, permission=SUPERUSER)
forget_cmd = on_command("profile_forget", priority=5, block=True, permission=SUPERUSER)
clear_cmd = on_command("profile_clear", priority=5, block=True, permission=SUPERUSER)
# 批次 4：C 的审计层门面。两条都带 SUPERUSER，群里任何人碰不到。
approve_cmd = on_command("approve", priority=5, block=True, permission=SUPERUSER)
audit_cmd = on_command("audit", priority=5, block=True, permission=SUPERUSER)


def _extract_arg(raw: str, cmd: str) -> str:
    name = cmd.lstrip("/／")
    return re.sub(rf"^[/／]?{re.escape(name)}(?:\s+|$)", "", (raw or "").strip(), count=1).strip()


def _arg(event: MessageEvent, cmd: str) -> str:
    """命令后面那一截参数。

    坑：`on_command` **不会**把命令本身从 `event.get_plaintext()` 里剥掉
    （`plugins/commands` 的 `_arg()` 存在就是为了剥它）。之前这里用
    `nonebot.get_current_event()` 取事件、又不剥前缀，两个问题叠在一起：
    取不到事件时静默返回空串，取到了也会把「/profile_forget 家庭」整体当字段名，
    于是这条命令永远只会回「用法：…」或「没有这个字段」。参数一律从 handler 的
    event 拿，别去猜当前事件。
    """
    return _extract_arg(event.get_plaintext() or "", cmd)


@approve_cmd.handle()
async def _approve(event: MessageEvent):
    """批准一个待授权的写操作，并**重放执行**（定义 01 §3 的「需授权」）。

    只认 10 分钟内的请求；过期 / 已处理 / 编的 id 一律不执行。
    """

    arg = _arg(event, "/approve")
    rid = arg.split()[0] if arg else ""
    if not rid:
        await approve_cmd.finish("用法：/approve <id>（/audit 看最近的记录）")
    item = core.audit.decide(rid, True, by="owner")
    if not item:
        await approve_cmd.finish("没有这个待授权请求（可能已过期或已处理）。")
    images: list[str] = []
    out = await core.tools.execute(core, str(item["name"]),
                                 dict(item.get("args") or {}), images=images, approved=True)
    for img in images:
        p = Path(img)
        if p.exists():
            data = await asyncio.to_thread(core.meme.payload, str(p))
            await approve_cmd.send(MessageSegment.image(data))
    await approve_cmd.finish(f"已批准并执行：{item['name']}\n{out[:200]}")


@audit_cmd.handle()
async def _audit_cmd():
    """只读：最近几条副作用、能不能回滚、哈希链是否完整、还有谁在等授权。"""

    rows = core.audit.recent(8)
    if not rows:
        await audit_cmd.finish("还没有审计记录。写类工具执行后会记一条。")
    ok, errors = core.audit.verify_chain(rows)
    lines = [f"最近 {len(rows)} 条副作用："]
    for r in rows:
        try:
            ts = datetime.fromtimestamp(float(r.get("ts") or 0)).strftime("%m-%d %H:%M")
        except Exception:  # noqa: BLE001
            ts = "—"
        flag = "可回滚" if r.get("revertible") else "不可回滚"
        lines.append(f"{ts} {str(r.get('status')):8} {r.get('action', '')} "
                     f"{str(r.get('target') or '')[:24]} [{flag}] {r.get('actor', '')}")
    lines.append("哈希链：" + ("完整" if ok else "异常 " + "; ".join(errors)[:80]))
    pend = core.audit.pending_ids()
    if pend:
        lines.append("待授权：" + ", ".join(pend[:5]) + "（/approve <id>）")
    await audit_cmd.finish("\n".join(lines))


@ping.handle()
async def _ping():
    """pong + 一行配置状态。

    配上意义：`admin.user_id` 没填时记忆不写、主动消息不发，但服务看着一切正常。
    /ping 是唯一能一眼看出「它其实是个哑巴」的地方，所以状态挂在这里。
    """
    await ping.finish(core.introspect.ping_text(core))


@help_cmd.handle()
async def _help():
    """/help 由 app_core/help.py 渲染：命令只写一处，lint 保证不漂。"""
    await help_cmd.finish(core.help.text())


@debug_cmd.handle()
async def _debug():
    """/debug：维护与排障命令全量。日常清单里只留了 11 条。"""
    await debug_cmd.finish(core.help.debug_text())


@reload_cmd.handle()
async def _reload(event: MessageEvent):
    core.reload_persona()
    await reload_cmd.finish(f"人格已重载（{len(core.persona.get('system_prompt', ''))} 字）")


@profile_cmd.handle()
async def _profile():
    """看当前画像：每个字段的置信度、多久没被确认、会不会被注入。"""
    await profile_cmd.finish(core.introspect.profile_text(core.learn))


@forget_cmd.handle()
async def _forget(event: MessageEvent):

    arg = _arg(event, "/profile_forget")
    if not arg:
        await forget_cmd.finish("用法：/profile_forget 职业与求职")
    if not core.profile.forget(core.learn.profile_path(), arg):
        keys = "、".join(core.learn.profile_fields().keys()) or "（空）"
        await forget_cmd.finish(f"没有这个字段。现有：{keys}")
    await forget_cmd.finish(f"已删掉「{arg}」，下次复盘不会自动加回来（除非又有新证据）")


@clear_cmd.handle()
async def _clear():

    core.profile.clear(core.learn.profile_path())
    await clear_cmd.finish("画像已清空。它会从下次复盘开始重新长，只记那时候看到的事。")
