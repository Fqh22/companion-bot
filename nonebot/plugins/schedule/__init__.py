"""schedule 插件：启动定时任务（日记/复盘/间隔重复/周报）+ 知识库索引。"""
from __future__ import annotations

import asyncio

from nonebot import get_bots
from nonebot.adapters.onebot.v11.exception import ActionFailed
from nonebot.log import logger

from app_core.delivery import DELIVERY_UNKNOWN, ConfirmedText, DeliveryResult, Receipt
from companion_core import shared_application

_application = shared_application()
core = _application.core

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


async def _send(text: str) -> DeliveryResult:
    """传输状态不明或部分送达时终止重试，避免重复推送已收到的内容。"""
    uid = _admin_id()
    if not uid:
        return False
    parts = _chunks(text)
    if not parts:
        return False
    for bot in get_bots().values():
        delivery = ConfirmedText()
        uncertain = False
        try:
            for p in parts:
                response = await bot.call_api("send_private_msg", user_id=uid, message=p)
                mid = response.get("message_id") if isinstance(response, dict) else None
                delivery.add(p, Receipt("qq", str(mid or ""), p))
                await asyncio.sleep(0.3)
        except ActionFailed as exc:
            logger.warning(f"[schedule] 平台拒绝推送，已送达 {len(delivery.parts)} 段：{exc}")
        except Exception as exc:
            uncertain = True
            logger.warning(f"[schedule] 推送结果未知：{type(exc).__name__}: {exc}")
        finally:
            if delivery.parts:
                await core.record_delivery(
                    f"private:{uid}", "\n".join(delivery.parts),
                    references=tuple(delivery.references), track_followup=False)
        if uncertain:
            return DELIVERY_UNKNOWN
        if delivery.parts:
            return True
    return False


sched = core.scheduler.Scheduler(core, send=_send)
_warm_tasks: list[asyncio.Task] = []


async def _start() -> None:
    if _warm_tasks:
        return
    sched.start()

    async def _warm() -> None:
        try:
            await core.kb.index()
        except Exception:
            pass
        try:
            await core.wiki.refresh(core)
        except Exception:
            pass

    _warm_tasks.extend((asyncio.create_task(_warm()),
                        asyncio.create_task(core.meme.ensure_vectors())))


async def _stop() -> None:
    await sched.stop()
    for task in _warm_tasks:
        task.cancel()
    await asyncio.gather(*_warm_tasks, return_exceptions=True)
    _warm_tasks.clear()


_application.register_background(_start, _stop)
