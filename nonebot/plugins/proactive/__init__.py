"""proactive 插件：主动消息的 QQ 壳。

频控、回应率、话题冷却、生成全在 app_core/proactive_runtime.py；
这里只回答一个问题：「这句话发给哪个 bot、发不出去怎么办」。
"""
from __future__ import annotations

import asyncio

from nonebot import get_bots, get_driver, logger

from app_core import config
from app_core.delivery import Receipt
from companion_core import shared_application

_application = shared_application()
core = _application.core

driver = get_driver()
_task: asyncio.Task | None = None


def _admin_id() -> int:
    try:
        return int(core.admin_uid)
    except (TypeError, ValueError):
        return 0


async def _send(text: str):
    # 默认仍是 QQ；改成 feishu 时优先走飞书注册通道，发不出去自动回退 QQ，
    # 不双发也不静默丢消息（#14）。
    try:
        
        channel = str((config.load().get("proactive") or {}).get(
            "channel", "qq")).strip().lower()
    except Exception:  # noqa: BLE001
        channel = "qq"
    if channel != "qq":
        receipt = await core.proactive_channels.send(text, channel)
        if receipt:
            logger.info("[proactive] 已由 %s 发送: %r", channel, text[:60])
            return receipt
        logger.warning("[proactive] 通道 %s 不可用，回退 QQ", channel)
    uid = _admin_id()
    if not uid:
        return False
    for bot in get_bots().values():
        try:
            response = await bot.call_api("send_private_msg", user_id=uid, message=text)
            logger.info("[proactive] 已发送: %r", text[:60])
            mid = response.get("message_id") if isinstance(response, dict) else None
            return Receipt("qq", str(mid or ""))
        except Exception as e:  # noqa: BLE001
            logger.warning("[proactive] 发送到 bot %s 失败：%s",
                           getattr(bot, "self_id", "?"), e)
    logger.error("[proactive] 没有可用的 bot，消息没发出去：%s", text[:40])
    return False


runtime = core.proactive_runtime.ProactiveRuntime(core, _send, admin_id=str(_admin_id()))


async def _tick() -> None:
    """兼容旧入口：smoke-plugins 和手工排障用。"""
    await runtime.tick()


async def _compose(topic: str, why: str, mood_hint: str = "") -> str:
    return await runtime.compose(topic, why, mood_hint)


@driver.on_startup
async def _start() -> None:
    global _task
    await _application.start(catch_up=True)
    if _task is not None and not _task.done():
        return
    try:
        runtime.init()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[proactive] 初始化表失败: {type(e).__name__}: {e}")
    _task = asyncio.create_task(runtime.run())


@driver.on_shutdown
async def _stop() -> None:
    global _task
    if _task is not None:
        _task.cancel()
        await asyncio.gather(_task, return_exceptions=True)
        _task = None
