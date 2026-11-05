"""schedule 插件：启动定时任务（日记/复盘/间隔重复/周报）+ 知识库索引。"""
from __future__ import annotations

import asyncio

from nonebot import get_bots, get_driver
from nonebot.log import logger

from app_core import kb, meme, wiki
from app_core.scheduler import Scheduler
from plugins.chat import core

driver = get_driver()


def _admin_id() -> int:
    """管理员 QQ 由 config.yaml 的 admin.user_id 决定，未配置时返回 0。"""
    try:
        return int(core.admin_uid)
    except (TypeError, ValueError):
        return 0


def _chunks(text: str, limit: int = 1200, max_parts: int = 3) -> list[str]:
    """按行/句切长消息，别从半句话中间截断。

    周报是「900 token 的模型输出 + 客观指标」，中文很容易超过 1200 字；
    以前 `text[:1200]` 无提示地切一半，用户收到的周报停在半句上。
    """
    text = (text or "").strip()
    if not text:
        return []
    parts: list[str] = []
    rest = text
    while rest and len(parts) < max_parts:
        if len(rest) <= limit:
            parts.append(rest)
            rest = ""
            break
        cut = rest.rfind("\n", 0, limit)
        if cut < limit // 2:
            try:
                cut = max(rest.rfind(c, 0, limit) for c in "。！？!?")
            except ValueError:
                cut = -1
        if cut < limit // 2:
            cut = limit - 1
        parts.append(rest[:cut + 1].strip())
        rest = rest[cut + 1:].strip()
    if rest:
        logger.error(f"[schedule] 推送超长，仍有 {len(rest)} 字没发出去")
        parts.append(f"…（还有 {len(rest)} 字没发出来）")
    return parts


async def _send(text: str) -> bool:
    """发一条私聊（超长自动分条）。返回是否真的发出去了。"""
    uid = _admin_id()
    if not uid:
        return False
    parts = _chunks(text)
    for bot in get_bots().values():
        sent: list[str] = []
        try:
            for p in parts:
                await bot.call_api("send_private_msg", user_id=uid, message=p)
                sent.append(p)
                await asyncio.sleep(0.3)
            # 提醒/推送同样要进历史，不然它「提醒过」这件事第二天就不记得了。
            # 记的是**用户实际收到的内容**（分条拼回去），不是原始全文。
            await core.record_assistant(f"private:{uid}", "\n".join(sent), "schedule")
            return True
        except Exception:  # noqa: BLE001
            continue
    return False


sched = Scheduler(core, send=_send)


@driver.on_startup
async def _start() -> None:
    sched.start()

    async def _warm() -> None:
        try:
            await kb.index()
        except Exception:
            pass
        try:
            await wiki.refresh(core)
        except Exception:
            pass

    asyncio.create_task(_warm())
    asyncio.create_task(meme.ensure_vectors())


@driver.on_shutdown
async def _stop() -> None:
    await sched.stop()
