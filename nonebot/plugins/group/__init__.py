"""group 插件：群聊只认 @，无记忆无情绪，失败静默。

和 `plugins/chat` 是两条独立链路——私聊那套会读写情绪、召回长期记忆、注入知识库，
这些能力在群里都是泄漏面。这里只做：@ → 限流 → 纯对话 → 发出去。
"""
from __future__ import annotations

import asyncio
import time

from nonebot import get_driver, logger, on_message
from nonebot.adapters.onebot.v11 import GroupMessageEvent

from app_core import pacing
from app_core.group import GroupCore, rate_ok

core = GroupCore()
driver = get_driver()
grp = on_message(priority=10, block=False)

_recent: dict[str, list[float]] = {}      # group_id -> 最近发送时刻


@driver.on_startup
async def _init() -> None:
    if not core.enabled:
        logger.info("[group] 群聊未启用（group.enabled=false）")
    elif not core.whitelist:
        logger.info("[group] 已启用但白名单为空 —— 空跑，任何群都不会回")
    else:
        logger.info(f"[group] 已启用，白名单 {len(core.whitelist)} 个群")


@grp.handle()
async def _handle(event: GroupMessageEvent):
    if not core.enabled:
        return
    if not isinstance(event, GroupMessageEvent):
        return
    gid = str(event.group_id)
    # 白名单为空 = 全关（空跑期就是这个状态，方便观察误触发）
    if not core.whitelist or gid not in core.whitelist:
        return
    # 只认 @。is_tome() 在 OneBot v11 里涵盖 @ 和回复两种，这里两种都接受
    if not event.is_tome():
        return

    text = event.get_plaintext().strip()
    if not text or text.startswith("/"):
        return

    now = time.time()
    times = _recent.setdefault(gid, [])
    times[:] = [t for t in times if now - t < 60]
    if not rate_ok(times, now, core.rate_per_minute, core.cooldown):
        logger.info(f"[group] {gid} 触发限流，丢弃一条")
        return
    if not core.budget_left():
        logger.info(f"[group] {gid} 今日 token 预算用尽，静默")
        return
    times.append(now)

    try:
        parts = await asyncio.wait_for(core.reply(gid, text), timeout=45)
    except Exception as e:  # noqa: BLE001
        # 群里绝不回错误信息：一句「出错了」比不说话更糟
        logger.warning(f"[group] reply failed（{gid}）：{type(e).__name__}: {e}")
        return
    if not parts:
        return

    total = len(parts)
    for i, part in enumerate(parts):
        if i:
            await asyncio.sleep(pacing.gap_seconds(i, total))
        try:
            await grp.send(part)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[group] send failed（{gid}）：{type(e).__name__}: {e}")
            return
