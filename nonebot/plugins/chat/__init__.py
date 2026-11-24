"""QQ 私聊接入、回复投递及附件处理。"""
from __future__ import annotations

import asyncio
import base64
import tempfile
import time
from datetime import datetime
from pathlib import Path

import httpx

from nonebot import get_bots, get_driver, logger, on_message
from nonebot.exception import FinishedException
from nonebot.adapters.onebot.v11 import (
    GroupMessageEvent,
    MessageEvent,
    MessageSegment,
    PrivateMessageEvent,
)

from app_core import asr, config, pacing
from app_core import turn as turn_mod
from app_core.inbound_turns import InboundTurns
from app_core.delivery import BUFFERED, Capability, DeliveryResult, Receipt
from app_core.runner import TurnRunner, TurnTiming
from app_core.outbound import READ_ALOUD_DIRECTIVE
from app_core.files import (MAX_IMAGES, parse, remember_pending,
                            save_from_segment, save_image)
from adapters.qq import to_inbound
from companion_core import shared_application

_application = shared_application()
core = _application.core
engine = _application.engine
QQ_CAPS = Capability(name="qq")
driver = get_driver()

_SETTLE_CFG = dict(config.load().get("chat") or {})
# 第一段是否引用他的消息。默认关：实测每条回复都挂引用框太重，只在长对话里才像接话。
QUOTE_REPLY = bool(_SETTLE_CFG.get("quote_reply", False))
# 语音回复形态：text_then_voice（旧）| voice_then_text | voice（只语音，失败退文字）
_VMODE = str(_SETTLE_CFG.get("voice_reply_mode") or "voice").strip().lower()
VOICE_REPLY_MODE = _VMODE if _VMODE in ("text_then_voice", "voice_then_text", "voice") else "voice"

chat = on_message(priority=10, block=False)


async def _draw_later(sid: str, ask: str) -> None:
    """后台画图并发送。公共编排在 app_core/outbound.py，这里只提供通道回调。"""
    from app_core import image_provider, outbound
    from app_core.message import Part

    await outbound.draw_later(
        ask=ask,
        generate=image_provider.generate,
        cleanup=image_provider._cleanup,
        send_image=lambda p: QQChannel().send_part(sid, Part("drawing", p)),
        send_text=lambda t: QQChannel().send_text(sid, t),
        record=lambda text, kind: core.record_assistant(sid, text, kind=kind),
        tag="[chat]",
    )


async def _speak_later(sid: str, text: str, parts: int, rewrite: bool = True) -> None:
    """后台合成语音并发送。公共编排在 app_core/outbound.py。"""
    from app_core import outbound, tts
    from app_core.message import Part

    ch = QQChannel()
    await outbound.speak_later(
        text=text,
        synth=lambda seg: tts.synth(seg, mood=core.mood.load(), rewrite=rewrite),
        send_voice=lambda p: ch.send_part(sid, Part("voice", p)),
        send_text=lambda t: ch.send_text(sid, t),
        record=lambda text, kind: core.record_assistant(sid, text, kind=kind),
        tag="[chat]",
    )


async def _deliver_voice_reply(sid: str, ch: QQChannel,
                             rewrite: bool = True, timing: TurnTiming | None = None) -> None:
    """voice / voice_then_text 形态：先语音，按配置决定要不要再补文字。"""
    from app_core import outbound, tts
    from app_core.message import Part

    buffered = list(ch.buffered)
    ch.defer_text = False
    await outbound.deliver_voice_reply(
        buffered=buffered,
        synth=lambda seg: tts.synth(seg, mood=core.mood.load(), rewrite=rewrite),
        send_voice=lambda p: ch.send_part(sid, Part("voice", p)),
        send_text=lambda t: ch.send_text(sid, t),
        record_delivery=lambda text, refs: core.record_delivery(sid, text, references=refs),
        mode=VOICE_REPLY_MODE,
        tag="[chat]",
        on_delivery=(lambda: timing.mark("first_sent")) if timing else None,
    )


def _add(sid: str, *, text: str | None = None, images: list[str] | None = None,
         file: tuple[str, str, str] | None = None,
         meta: dict | None = None) -> None:
    _turns.submit(sid, turn_mod.Turn(texts=[text] if text else [],
                                    images=images or [], file=file, meta=dict(meta or {})))
    try:
        core.note_user_active(sid)
    except Exception:  # noqa: BLE001
        pass


class QQChannel:
    """QQ 的通道实现：IO 原语 + 能力声明，会话循环交给 app_core.runner。"""

    forms = {"text", "image", "voice", "drawing"}

    def __init__(self, reply_to: str | None = None) -> None:
        self.sent: list[str] = []
        # 引用回复要引用「本轮」那条消息：跟着实例走，不能放模块级字典。
        # 放字典里会在拿会话锁之前就被下一条排队消息覆盖（同会话竞态）。
        self.reply_to = str(reply_to or "") or None
        # 语音形态下先压住文字：voice / voice_then_text 时攒起来，等语音发完再处理
        self.defer_text = False
        self.buffered: list[str] = []

    async def send_part(self, session: str, part) -> DeliveryResult:
        """协议入口：按 form 分派到具体原语。

        加「画画」时这里不用改——画出来的图就是 image。
        """
        if part.form == "text":
            return await self.send_text(session, part.content)
        if part.form in ("image", "drawing"):
            # 画出来的图就是一张图，通道不需要区分「生成」还是「已有」
            return await self.send_image(session, part.content)
        if part.form == "voice":
            return await self.send_voice(session, part.content)
        return False

    async def send_text(self, session: str, text: str) -> DeliveryResult:
        if self.defer_text:
            # 已缓冲 ≠ 已送达，也 ≠ 发送失败。返回 BUFFERED：引擎不要再补发
            # 一遍，历史仍等语音/文字兜底真正成功后再写。
            self.buffered.append(text)
            return BUFFERED
        # 只有第一段带引用：后面几段接在它自己那条后面才对
        reply_id = self.reply_to if (QUOTE_REPLY and not self.sent) else None
        ok = await _send_text(text, reply_id=reply_id)
        if ok:
            self.sent.append(text)
        return ok

    async def send_image(self, session: str, path: str) -> bool:
        try:
            p = Path(path)
            if not p.exists():
                logger.warning(f"[chat] 表情包文件不存在：{p}")
                return False
            # 传 bytes → 适配器编码成 base64；传 Path 会变成 file:// 宿主机路径，
            # 而 OneBot 跑在容器里看不到宿主机目录（实测 ENOENT，表情包一张都发不出去）。
            data = await asyncio.to_thread(core.meme.payload, str(p))
            await chat.send(MessageSegment.image(data))
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[chat] 表情包发送失败：{type(e).__name__}: {e}")
            return False

    async def send_voice(self, session: str, path: str) -> bool | Receipt:
        try:
            p = Path(path)
            if not p.exists():
                logger.warning(f"[chat] 语音文件不存在：{p}")
                return False
            # image(bytes) 会自动转 base64，但 record 不一定——实测直接把
            # bytes 传进去 QQ 播不了。这里显式编成 base64:// 前缀。
            data = await asyncio.to_thread(p.read_bytes)
            b64 = base64.b64encode(data).decode("ascii")
            response = await chat.send(MessageSegment.record(f"base64://{b64}"))
            mid = response.get("message_id") if isinstance(response, dict) else None
            return Receipt("qq", str(mid or ""))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[chat] 语音发送失败：{type(e).__name__}: {e}")
            return False

    async def send_progress(self, session: str, text: str) -> bool:
        return bool(await _send_text(text))

    async def typing(self, session: str, on: bool) -> None:
        await _set_typing(session, on)


async def _run_turn(sid: str, turn: turn_mod.Turn) -> None:
    timing = TurnTiming("qq", received=turn.meta.get("received_at", time.monotonic()))
    timing.input_count = turn.meta["input_count"]
    timing.mark("batch_start")
    timing.marks["input_ready"] = turn.meta.get("ready_at", timing.received)
    prompt, stored = turn.compose()
    raw = "\n".join(turn.texts or [])
    voice = bool(turn.meta.get("voice"))
    reply_to = turn.meta.get("reply_to")
    quoted = turn.meta.get("quote_text")
    # 引用 ID 跟本轮实例走：下一条消息的 turn 再创建自己的 ch，互不覆盖。
    ch = QQChannel(reply_to=turn.meta.get("event_id"))
    runner = TurnRunner(engine, ch, caps=QQ_CAPS, deadline=core.reply_deadline,
                        stall_seconds=getattr(core, "reply_stall_seconds", 30), timing=timing)
    logger.info(f"[chat] recv {sid}: {prompt[:80]!r}"
                f"{'（含 %d 张图）' % len(turn.images) if turn.images else ''}")
    try:
        timing.mark("intent_start")
        decision = await core.interpret_intent(
            sid, raw, voice=voice, quote_text=quoted,
            reply_to=f"qq:{reply_to}" if reply_to else None)
        timing.mark("intent_finish")
        timing.reply_form = "voice" if decision.voice else "text"
        ch.defer_text = decision.voice and VOICE_REPLY_MODE != "text_then_voice"
        model_text = READ_ALOUD_DIRECTIVE + prompt if decision.read_aloud else prompt
        msg = to_inbound(
            session=sid,
            user_id=str(turn.meta.get("user_id") or ""),
            chat_id=turn.meta.get("chat_id"),
            is_group=bool(turn.meta.get("is_group")),
            text=model_text, images=list(turn.images or []),
            event_id=turn.meta.get("event_id"),
            reply_to=reply_to,
            quote_text=quoted,
            store_text=stored,
            voice=voice, intent=decision)
        out = await runner.run(msg)
        # 行号要在锁内取：出了锁，下一轮可能已经把它覆盖
        msg_id = int(getattr(core, "user_msg_id", 0) or 0)
        logger.info(f"[chat] sent {sid}: {len(ch.sent)} 段（含补发）")
        if getattr(out, "images_sent", None):
            # 只看**实际发成功**的图；发送失败时不能写「另外发了一张图」。
            try:
                await core.record_assistant(
                    sid, "（另外发了一张图）", kind="image")
            except Exception:  # noqa: BLE001
                pass
        if decision.voice and ch.buffered:
            await _deliver_voice_reply(sid, ch, rewrite=not decision.read_aloud, timing=timing)
        elif decision.voice and ch.sent:
            asyncio.create_task(
                _speak_later(sid, "".join(ch.sent), len(ch.sent), rewrite=not decision.read_aloud))
        # 画画同理：只在他明确要求时画，不做「氛围到了就画」
        if decision.draw:
            asyncio.create_task(_draw_later(sid, raw))
        if turn.images and msg_id:
            # 图注放后台：它只影响「之后的轮次」，晚一两秒不影响这一条回复
            asyncio.create_task(_caption_later(msg_id, list(turn.images)))
    except asyncio.TimeoutError:
        timing.status = "timeout"
        logger.error(f"[chat] 回复收尾超时（总上限 {core.reply_deadline:.0f}s） "
                     f"{sid}，已发 {len(ch.sent)} 段")
        await core.mood_event("timeout")
        if ch.sent:
            _tail = "先说到这。"
            # 顺序反过来：**先发**收尾，发成功了才写历史。以前先写后发，
            # 发送失败时历史里就多了一句用户从没见过的「先说到这」。
            # 已发出的正文由 runner 在超时路径上写回，这里只补收尾这一句。
            try:
                await chat.finish(_tail)
                await core.remember_safe(sid, "assistant", _tail)
                logger.info(f"[chat] 超时补记收尾（已发 {len(ch.sent)} 段）")
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[chat] 超时补发/补记失败：{e}")
        else:
            try:
                await chat.finish("我这会儿有点卡，你再说一遍？")
            except Exception:
                pass
    except Exception as e:  # noqa: BLE001
        timing.status = "error"
        logger.warning(f"[chat] reply failed: {e}")
        try:
            await chat.finish(f"出错了：{type(e).__name__}")
        except Exception:
            pass
    finally:
        timing.emit()


_turns = InboundTurns(_run_turn,
                     text_gap=float(_SETTLE_CFG.get("text_settle_seconds", 2.0)),
                     media_gap=float(_SETTLE_CFG.get("media_settle_seconds", 1.5)))


async def _caption_later(msg_id: int, paths: list[str]) -> None:
    """后台给图片补一句图注，追加到那条用户消息上。

    为什么值得多花一次小调用：历史里原来只写「（附了 1 张图）」，隔天问
    「昨天那张图」它完全不记得。图注把「图」变成可检索的文字，也让复盘时
    能看见「当时我看到的是 X，他说不是这意思」。
    """
    try:
        cap = await core.caption_images(paths)
        if cap and await core.append_to_message(msg_id, f"｜图注：{cap}"):
            logger.info(f"[chat] 图注已补：{cap[:30]}")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[chat] 补图注失败：{type(e).__name__}: {e}")


def _bot():
    """取当前连上的 bot。

    NoneBot2 的 event 上没有 .bot 属性（AstrBot 那套才有），
    要用 get_bots()。之前文件/语音处理写 event.bot，一用就 AttributeError。
    """
    bots = get_bots()
    return next(iter(bots.values()), None)


def _session_key(event: MessageEvent) -> str:
    if isinstance(event, GroupMessageEvent):
        return f"group:{event.group_id}"
    return f"private:{event.user_id}"


async def _set_typing(sid: str, on: bool) -> None:
    """私聊里显示「正在输入」。SnowLuma 支持 set_input_status；失败静默降级。

    为什么值得做：这是整条链路里唯一一个「对方也在场」的可见信号。
    以前只有发送前的 sleep（假装打字），但用户看不到任何状态。
    """
    if not sid.startswith("private:"):
        return
    bot = _bot()
    if bot is None:
        return
    try:
        await bot.call_api("set_input_status",
                           user_id=int(sid.split(":", 1)[1]),
                           event_type=1 if on else 0)
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[chat] typing 设置失败：{type(e).__name__}: {e}")


async def _typing_loop(sid: str) -> None:
    """QQ 的「正在输入」会自己过期，5 秒续一次，直到本轮结束。"""
    try:
        while True:
            await _set_typing(sid, True)
            await asyncio.sleep(5)
    except asyncio.CancelledError:
        raise


async def _pace(text: str, fast: bool = False) -> None:
    """发出这一条之前的“打字”停顿。计算在 app_core.pacing，这里只负责睡。

    fast=True 时压低上限：长回答段数多，再按原节奏发就会撞穿回复时限。
    """
    await asyncio.sleep(pacing.pace_seconds(text, datetime.now().hour,
                                            core.mood.load(), fast=fast))


async def _send_text(part: str, tries: int = 2, reply_id: str | None = None) -> DeliveryResult:
    """发一条文字，失败重试一次。发不出去不抛异常——用户侧无能为力，
    但必须留日志，否则「它回了但没收到」永远查不出来。

    reply_id 非空时给第一条加引用：让回复看起来是「接着你这句话」，
    而不是悬空一句。引用失败（实现不支持 / id 非法）自动退回纯文本。
    """
    msg = part
    if reply_id:
        try:
            msg = MessageSegment.reply(int(reply_id)) + part
        except Exception:  # noqa: BLE001
            msg = part
    for attempt in range(1, tries + 1):
        try:
            response = await chat.send(msg)
            mid = response.get("message_id") if isinstance(response, dict) else None
            return Receipt("qq", str(mid or ""), part)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[chat] 发送失败（{attempt}/{tries}）：{type(e).__name__}: {e}")
            await asyncio.sleep(0.5 * attempt)
    return False


async def _send_replies(replies: list[str], images: list[str] | None = None,
                        reply_id: str | None = None) -> None:
    total = len(replies)
    for i, part in enumerate(replies):
        if i:
            await asyncio.sleep(pacing.gap_seconds(i, total))
        await _pace(part, fast=pacing.is_fast(i, total))
        await _send_text(part, reply_id=reply_id if i == 0 else None)
    for path in images or []:
        try:
            p = Path(path)
            if not p.exists():
                logger.warning(f"[chat] 表情包文件不存在：{p}")
                continue
            # 传 bytes → 适配器编码成 base64；传 Path 会变成 file:// 宿主机路径，
            # 而 OneBot 跑在容器里看不到宿主机目录（实测 ENOENT，表情包一张都发不出去）。
            data = await asyncio.to_thread(core.meme.payload, str(p))
            await chat.send(MessageSegment.image(data))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[chat] 表情包发送失败：{type(e).__name__}: {e}")


async def _collect_file(sid: str, seg: MessageSegment, caption: str = "",
                        owner: bool = False, meta: dict | None = None) -> None:
    """下载并解析文件，塞进这一轮的队列（不直接回话）。

    caption 是同一条消息里他自己打的字（「帮我看看这份简历」），
    和文件正文一起进这一轮——少了它，模型的提问上下文就断了。
    """
    name = str((getattr(seg, "data", None) or {}).get("file") or "文件")
    bot = _bot()
    if bot is None:
        await chat.finish("机器人没连上，稍后再试")
        return
    path, name = await save_from_segment(bot, seg)
    if path is None:
        await chat.finish("文件没拿到，可能太大了或者格式不支持")
        return
    # 只有主人发的文件才登记成「下一条指令可以带上」。
    if owner:
        remember_pending(path, name)
    # 解析放到线程里并限时：pdf/xlsx 遇到畸形文件能把进程卡死
    try:
        body, note = await asyncio.wait_for(asyncio.to_thread(parse, path), timeout=60)
    except asyncio.TimeoutError:
        await chat.finish("文件解析超时了，换个小点的或者直接发文本吧")
        return
    if not body.strip():
        await chat.finish(f"读不了这个文件：{note or '内容为空'}")
        return
    logger.info(f"[chat] file {name} -> {len(body)} chars ({note})")
    _add(sid, text=caption.strip() or None, file=(name, body, note), meta=meta)


async def _collect_voice(sid: str, seg, text: str,
                         meta: dict | None = None) -> None:
    """语音：下载 → 转码 → 智谱转写 → 当文本进对话（docs/01-定义与决策.md）。

    转不出来就明说，不猜内容——一条语音听不清，不该变成一段编出来的话。
    """
    url = str(seg.data.get("url") or seg.data.get("file") or "")
    if not url.startswith("http"):
        await chat.finish("语音没拿到链接，打字说吧")
        return
    tmp = None
    try:
        async with httpx.AsyncClient(
                timeout=60, follow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0 (Linux; Android 13) QQ/9.0"}) as c:
            r = await c.get(url)
        if r.status_code != 200:
            logger.warning(f"[chat] voice 下载失败 {r.status_code} {url[:60]}")
            await chat.finish("语音下载失败，打字吧")
            return
        tmp = Path(tempfile.mkstemp(prefix="voice-")[1])
        tmp.write_bytes(r.content)
        wav = await asyncio.to_thread(asr.to_wav, tmp)
        if wav is None:
            logger.warning(f"[chat] voice 转码失败 {len(r.content)} 字节")
            await chat.finish("这段语音我转不了，打字吧")
            return
        res = await asr.transcribe(wav)
        wav.unlink(missing_ok=True)
        heard = str(res.get("text") or "").strip()
        if not heard:
            logger.warning(f"[chat] voice 空转写：{res.get('error')}")
            await chat.finish("没听清，再说一遍？")
            return
        logger.info(f"[chat] voice {sid}: {len(heard)} 字")
        # 形式进 meta，不再塞进正文：前缀是渲染细节，不该变成数据。
        # 渲染层（Turn.compose）仍然会加「[语音] 」，模型看到的没变。
        _m = dict(meta or {})
        _m["voice"] = True
        _add(sid, text=(text + " " + heard).strip() if text else heard, meta=_m)
    except FinishedException:
        raise
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[chat] voice failed: {type(e).__name__}: {e}")
        await chat.finish("语音没听清，打字吧")
    finally:
        if tmp:
            tmp.unlink(missing_ok=True)


async def _collect_images(sid: str, segs: list, text: str,
                          meta: dict | None = None) -> None:
    """下载图片，和同一条消息里的文字一起塞进队列。"""
    bot = _bot()
    if bot is None:
        await chat.finish("机器人没连上，稍后再试")
        return
    paths = []
    for seg in segs[:MAX_IMAGES]:
        try:
            p = await save_image(bot, seg)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[chat] image save failed: {e}")
            p = None
        if p:
            paths.append(p)
    if not paths:
        await chat.finish("图片没拿到，可能太大了或者格式不支持")
        return
    logger.info(f"[chat] recv {sid}: {len(paths)} 张图 + {len(text)} 字")
    _add(sid, images=paths, text=text or None, meta=meta)


@chat.handle()
async def _handle(event: MessageEvent):
    received = time.monotonic()
    # 群消息交给 plugins/group（另一条链路：无记忆、无情绪、限流、静默失败）。
    # 两条链路共用一个 handler 迟早出事——私聊那套会往私有记忆里写群友的话。
    if not isinstance(event, PrivateMessageEvent):
        return

    sid = _session_key(event)
    text = event.get_plaintext().strip()
    _is_group = isinstance(event, GroupMessageEvent)
    _reply = getattr(event, "reply", None)
    _quoted = ""
    if _reply is not None:
        _qmsg = getattr(_reply, "message", None)
        if _qmsg is not None and hasattr(_qmsg, "extract_plain_text"):
            _quoted = _qmsg.extract_plain_text()
        else:
            _quoted = str(_qmsg or "")
    meta = {
        "received_at": received,
        "user_id": str(event.user_id),
        "chat_id": str(event.group_id) if _is_group else None,
        "is_group": _is_group,
        "event_id": str(getattr(event, "message_id", "") or ""),
        "reply_to": str(getattr(_reply, "message_id", "") or "") or None,
        "quote_text": (_quoted or "").strip()[:200] or None,
    }
    # 身份判定统一走 core（含 admin.identities 与平台映射）；缺省时不是主人。
    # 以前这里把空 admin 当主人，等于绕过 core 的口径自己开了一道门。
    _owner = core.is_owner(sid)

    # 外人版人格写着「0 点到 8 点不回复」。只对别人生效——主人半夜找它不该被晾着。
    if not _owner and core.relational.is_quiet_hour(datetime.now().hour):
        return

    file_segs = [s for s in event.message if getattr(s, "type", "") == "file"]
    if file_segs:
        await _collect_file(sid, file_segs[0], text, owner=_owner, meta=meta)
        return

    rec_segs = [s for s in event.message if getattr(s, "type", "") == "record"]
    if rec_segs:
        await _collect_voice(sid, rec_segs[0], text, meta=meta)
        return

    img_segs = [s for s in event.message if getattr(s, "type", "") == "image"]
    if img_segs:
        await _collect_images(sid, img_segs, text, meta=meta)
        return

    if not text or text.startswith("/"):
        return

    _add(sid, text=text, meta=meta)
