"""chat 插件：消息接入、排队合并、分段、节奏、文件、表情包。

## 为什么是「队列」而不是「等待式防抖」

旧做法是收到消息先睡 1.1 秒，指望这期间再来一条好合并。实测 14 天 472 次连发：
**一次都没合并成功**（最短间隔 1.1s，多数在 20 秒以上），却让每条消息白等 1.05 秒。
等待式防抖对「一句一句发、间隔二十秒起」的人纯属空转。

现在改成会话级队列 + 边答边接：

- 文本、图片、文件走**同一个入口**，先进 `TurnQueue`，再统一由 `_pump_loop` 处理。
  旧实现里图片和文件是「立即处理并 return」的近路，所以「发图 + 补一句」会被拆成
  两条互不知情的回复——这就是那个 bug 的根因。
- **纯文本零等待**，立刻开始生成。
- **图片/文件等最多 1.5 秒**，看他有没有补一句；期间来了就合并成一轮。
  图片本来就少，1.5 秒换「图和说明一起理解」很划算。
- **生成期间来的消息排队**，当前轮一结束立刻接着处理，不丢、不额外等。
- 同一会话用锁串行，保证历史顺序不乱。
"""
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

from app_core import asr, config, meme, mood, pacing, relational
from app_core import turn as turn_mod
from app_core.chat import ChatCore
from app_core.delivery import Capability
from app_core.engine import Engine
from app_core.runner import TurnRunner
from app_core.files import (MAX_IMAGES, parse, remember_pending,
                            save_from_segment, save_image)
from adapters.qq import to_inbound

core = ChatCore()
engine = Engine(core)
QQ_CAPS = Capability(name="qq")
driver = get_driver()

_SETTLE_CFG = dict(config.load().get("chat") or {})
SETTLE_MEDIA = float(_SETTLE_CFG.get("media_settle_seconds", 1.5))
# 第一段是否引用他的消息。默认关：实测每条回复都挂引用框太重，只在长对话里才像接话。
QUOTE_REPLY = bool(_SETTLE_CFG.get("quote_reply", False))
# 语音回复形态：text_then_voice（旧）| voice_then_text | voice（只语音，失败退文字）
_VMODE = str(_SETTLE_CFG.get("voice_reply_mode") or "voice").strip().lower()
VOICE_REPLY_MODE = _VMODE if _VMODE in ("text_then_voice", "voice_then_text", "voice") else "voice"

_locks: dict[str, asyncio.Lock] = {}

chat = on_message(priority=10, block=False)


def _lock(sid: str) -> asyncio.Lock:
    """同一会话串行处理：否则两次并发 reply 会打乱历史顺序、重复召回。"""
    lk = _locks.get(sid)
    if lk is None:
        lk = asyncio.Lock()
        _locks[sid] = lk
    return lk


_turns = turn_mod.TurnQueue()
_pump: dict[str, asyncio.Task] = {}


# 画图的触发词：动词 + 量词组合，避免「画的」「画面」这类误触发
_DRAW_TRIGGERS = ("画一张", "画个", "画幅", "画一下", "帮我画", "给我画")


async def _draw_later(sid: str, ask: str) -> None:
    """后台画图并发送。画不出来就说一句，不打断主回复。"""
    try:
        from app_core import image_provider
        from app_core.message import Part

        path = await image_provider.generate(ask)
        if not path:
            # 文字回复可能已经说了「画好了」：静默会让用户以为机器人坏了
            try:
                await QQChannel().send_text(sid, "这次没画出来，等下再试。")
            except Exception:  # noqa: BLE001
                pass
            logger.info("[chat] 画图返回空（额度/内容审核/网络），已补一句提示")
            return
        image_provider._cleanup(path)
        # 形式是 drawing（生成图）；通道侧再映射成图片发送
        ok = await QQChannel().send_part(sid, Part("drawing", str(path)))
        logger.info(f"[chat] drawing sent={ok} {sid} {path.stat().st_size} 字节")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[chat] 画图链路失败：{type(e).__name__}: {e}")


async def _speak_later(sid: str, text: str, parts: int) -> None:
    """后台合成语音并发送。

    只读最后一段：几段拼起来读会像播报，而语音本来就该说一句。
    失败静默——文字已经发出去了，语音是补充不是替代。
    """
    try:
        from app_core import tts

        # 读完整回复（截 300 字），不再只取最后一段——
        # 实测 85 字的回复最后一段只有 10 字，听上去像被掐断。
        say = (text or "").strip()
        if len(say) > 300:
            say = say[:300]
        silk = await tts.synth(say, mood=mood.load())
        if not silk:
            logger.info("[chat] 语音合成返回空，跳过")
            return
        # 走协议入口而不是直接调 send_voice：加形式只改 adapter。
        # 但这条链路在 runner 之外（文字回复已经发完才合成语音），所以失败要自己
        # 补一句——他明确要了语音，静默等于漏回。
        from app_core.message import Part

        ch = QQChannel()
        ok = await ch.send_part(sid, Part("voice", str(silk)))
        if not ok:
            try:
                await ch.send_text(sid, "（语音没发出去，文字顶上）")
            except Exception:  # noqa: BLE001
                pass
        logger.info(f"[chat] voice sent={ok} {sid} {len(say)} 字")
        if ok:
            # 让它知道"我刚发了语音"：否则历史里只有文字，问起来它会否认。
            # 单独记一条极短的助手旁注，比改正文安全（不污染回复内容）。
            try:
                await core.record_assistant(
                    sid, "（这句话我也用语音说了一遍）", kind="voice")
            except Exception:  # noqa: BLE001
                pass
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[chat] 语音链路失败：{type(e).__name__}: {e}")


async def _deliver_voice_reply(sid: str, ch: QQChannel) -> None:
    """voice / voice_then_text 形态：先语音，按配置决定要不要再补文字。"""
    from app_core import tts
    from app_core.message import Part

    buffered = list(ch.buffered)
    ch.defer_text = False
    say = "".join(buffered).strip()[:300]
    ok = False
    try:
        silk = await tts.synth(say, mood=mood.load()) if say else None
        if silk:
            ok = await ch.send_part(sid, Part("voice", str(silk)))
            silk.unlink(missing_ok=True)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[chat] 语音发送失败，退回文字：{e}")
    if ok:
        try:
            await core.record_assistant(
                sid, "（这句话我也用语音说了一遍）", kind="voice")
        except Exception:  # noqa: BLE001
            pass
    if not ok or VOICE_REPLY_MODE == "voice_then_text":
        for seg in buffered:
            try:
                await ch.send_text(sid, seg)
            except Exception:  # noqa: BLE001
                pass
    logger.info(f"[chat] voice_reply mode={VOICE_REPLY_MODE} ok={ok} {sid}")


def _add(sid: str, *, text: str | None = None, images: list[str] | None = None,
         file: tuple[str, str, str] | None = None,
         meta: dict | None = None) -> None:
    _turns.add(sid, text=text, images=images, file=file, meta=meta)


def _take(sid: str) -> turn_mod.Turn:
    return _turns.take(sid)


def _kick(sid: str) -> None:
    """有活就起一个泵；已经在跑就交给它——它会在收尾前再检查一次队列。"""
    task = _pump.get(sid)
    if task is not None and not task.done():
        return
    _pump[sid] = asyncio.create_task(_pump_loop(sid))




async def _settle(sid: str, base: int, timeout: float) -> None:
    """等 timeout 秒，或者等到又来了新输入——谁先到算谁。"""
    if timeout <= 0:
        return
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _turns.hits(sid) > base:
            return
        await asyncio.sleep(0.05)


async def _pump_loop(sid: str) -> None:
    try:
        while True:
            turn = _turns.peek(sid)
            if turn is None or turn.empty():
                return
            # 图片/文件之后往往还有一句说明，给它一个短窗口；期间来了就一起处理
            if (turn.images or turn.file) and not turn.texts:
                await _settle(sid, _turns.hits(sid), SETTLE_MEDIA)
            turn = _take(sid)
            if turn.empty():
                return
            # 观测：「生成期间又来了一条」。这里记的是**真·生成中到达**
            # （settle 期间到达的会在 _take 之前被合并，不会落进这个分支）。
            # 攒两周数据再决定要不要动落库时序去支持"真合并"。
            if _turns.has(sid):
                logger.info("[chat] 生成期间收到补充，已排队为下一轮")
            await _run_turn(sid, turn)
    finally:
        # 只摘掉自己那一格：收尾瞬间可能已经有新泵被 _kick 起出来了，
        # 无脑 pop 会把新任务从登记表里抹掉，下一条消息就会再起一个泵 → 双回复。
        if _pump.get(sid) is asyncio.current_task():
            _pump.pop(sid, None)
        if _turns.has(sid):
            _kick(sid)          # 收尾竞态：退出瞬间又来了消息


class QQChannel:
    """QQ 的通道实现：IO 原语 + 能力声明，会话循环交给 app_core.runner。"""

    forms = {"text", "image", "voice", "drawing"}

    def __init__(self) -> None:
        self.sent: list[str] = []
        # 语音形态下先压住文字：voice / voice_then_text 时攒起来，等语音发完再处理
        self.defer_text = False
        self.buffered: list[str] = []

    async def send_part(self, session: str, part) -> bool:
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

    async def send_text(self, session: str, text: str) -> bool:
        if self.defer_text:
            self.buffered.append(text)
            self.sent.append(text)
            return True
        # 只有第一段带引用：后面几段接在它自己那条后面才对
        reply_id = None
        if QUOTE_REPLY and not self.sent:
            reply_id = _current_event_id.get(session)
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
            data = await asyncio.to_thread(meme.payload, str(p))
            await chat.send(MessageSegment.image(data))
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[chat] 表情包发送失败：{type(e).__name__}: {e}")
            return False

    async def send_voice(self, session: str, path: str) -> bool:
        try:
            p = Path(path)
            if not p.exists():
                logger.warning(f"[chat] 语音文件不存在：{p}")
                return False
            # image(bytes) 会自动转 base64，但 record 不一定——实测直接把
            # bytes 传进去 QQ 播不了。这里显式编成 base64:// 前缀。
            data = await asyncio.to_thread(p.read_bytes)
            b64 = base64.b64encode(data).decode("ascii")
            await chat.send(MessageSegment.record(f"base64://{b64}"))
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[chat] 语音发送失败：{type(e).__name__}: {e}")
            return False

    async def typing(self, session: str, on: bool) -> None:
        await _set_typing(session, on)


# 引用回复要用「本轮用户消息的 id」，而 runner 只认 Channel 接口。
# 用一个按会话存放的临时表传进去，避免把 QQ 的 event 概念泄进 app_core。
_current_event_id: dict[str, str] = {}


async def _run_turn(sid: str, turn: turn_mod.Turn) -> None:
    prompt, stored = turn.compose()
    # 语音回复只在两种情况下发：他发语音，或者他明确要求。要在跑之前知道，
    # 才能决定本轮文字是先压住（voice/voice_then_text）还是照旧流式发（旧行为）。
    _want_voice = (stored or "").startswith("[语音]") or any(
        k in (prompt or "") for k in ("用语音", "语音说", "发语音", "读给我听", "念给我听"))
    ch = QQChannel()
    ch.defer_text = _want_voice and VOICE_REPLY_MODE != "text_then_voice"
    runner = TurnRunner(engine, ch, caps=QQ_CAPS, deadline=core.reply_deadline)
    logger.info(f"[chat] recv {sid}: {prompt[:80]!r}"
                f"{'（含 %d 张图）' % len(turn.images) if turn.images else ''}")
    if turn.meta.get("event_id"):
        _current_event_id[sid] = str(turn.meta["event_id"])
    try:
        async with _lock(sid):
            msg = to_inbound(
                session=sid,
                user_id=str(turn.meta.get("user_id") or ""),
                chat_id=turn.meta.get("chat_id"),
                is_group=bool(turn.meta.get("is_group")),
                text=prompt, images=list(turn.images or []),
                event_id=turn.meta.get("event_id"),
                reply_to=turn.meta.get("reply_to"),
                store_text=stored,
                voice=bool(turn.meta.get("voice")))
            out = await runner.run(msg)
            # 行号要在锁内取：出了锁，下一轮可能已经把它覆盖
            msg_id = int(getattr(core, "user_msg_id", 0) or 0)
        logger.info(f"[chat] sent {sid}: {len(ch.sent)} 段（含补发）")
        if _want_voice and ch.buffered:
            await _deliver_voice_reply(sid, ch)
        elif _want_voice and ch.sent:
            asyncio.create_task(_speak_later(sid, "".join(ch.sent), len(ch.sent)))
        # 画画同理：只在他明确要求时画，不做「氛围到了就画」
        _ask = " ".join(turn.texts or [])
        if any(k in _ask for k in _DRAW_TRIGGERS):
            asyncio.create_task(_draw_later(sid, _ask))
        if turn.images and msg_id:
            # 图注放后台：它只影响「之后的轮次」，晚一两秒不影响这一条回复
            asyncio.create_task(_caption_later(msg_id, list(turn.images)))
    except asyncio.TimeoutError:
        logger.error(f"[chat] 回复超时（>{core.reply_deadline:.0f}s） {sid}，已发 {len(ch.sent)} 段")
        await core.mood_event("timeout")
        if ch.sent:
            # 用户已经看到这几句了，必须写回历史：否则下一轮它完全不知道自己说过什么，
            # 而 summary 也会把这条用户消息当成"没人回过"。
            try:
                await core.remember_safe(sid, "assistant", "".join(ch.sent))
                logger.info(f"[chat] 超时补记已发出的 {len(''.join(ch.sent))} 字")
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[chat] 超时补记失败：{e}")
        if not ch.sent:
            try:
                await chat.finish("我这会儿有点卡，你再说一遍？")
            except Exception:
                pass
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[chat] reply failed: {e}")
        try:
            await chat.finish(f"出错了：{type(e).__name__}")
        except Exception:
            pass
    finally:
        _current_event_id.pop(sid, None)


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


@driver.on_startup
async def _init() -> None:
    await core.init_db()
    # 静默失效的配置在这里喊出来：admin.user_id 没配时记忆不写、主动消息不发，
    # 而且不抛任何异常，只能靠启动日志和 /ping 发现。
    for issue in core.config_issues():
        logger.error(f"[config] {issue}")
    logger.info(f"chat core ready（{core.config_status()}）")
    asyncio.create_task(_catch_up())


async def _catch_up() -> None:
    """启动补偿总结：把崩溃窗口里还没固化的对话补进长期记忆。"""
    try:
        r = await core.catch_up_summary()
        if r.get("ran"):
            logger.info(f"[summary] 启动补偿：补了 {r.get('pending')} 轮未总结的对话")
        elif r.get("reason") == "no-admin":
            logger.warning("[summary] 启动补偿跳过：admin.user_id 未配置")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[summary] 启动补偿失败：{type(e).__name__}: {e}")


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
                                            mood.load(), fast=fast))


async def _send_text(part: str, tries: int = 2, reply_id: str | None = None) -> bool:
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
            await chat.send(msg)
            return True
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
            data = await asyncio.to_thread(meme.payload, str(p))
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
    """语音：下载 → 转码 → 智谱转写 → 当文本进对话（docs/10）。

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
    # 群消息交给 plugins/group（另一条链路：无记忆、无情绪、限流、静默失败）。
    # 两条链路共用一个 handler 迟早出事——私聊那套会往私有记忆里写群友的话。
    if not isinstance(event, PrivateMessageEvent):
        return

    sid = _session_key(event)
    text = event.get_plaintext().strip()
    _is_group = isinstance(event, GroupMessageEvent)
    _reply = getattr(event, "reply", None)
    meta = {
        "user_id": str(event.user_id),
        "chat_id": str(event.group_id) if _is_group else None,
        "is_group": _is_group,
        "event_id": str(getattr(event, "message_id", "") or ""),
        "reply_to": str(getattr(_reply, "message_id", "") or "") or None,
    }
    _admin = str((config.load().get("admin") or {}).get("user_id") or "0")
    _owner = _admin in ("", "0") or str(event.user_id) == _admin

    # 外人版人格写着「0 点到 8 点不回复」。只对别人生效——主人半夜找它不该被晾着。
    if not _owner and relational.is_quiet_hour(datetime.now().hour):
        return

    file_segs = [s for s in event.message if getattr(s, "type", "") == "file"]
    if file_segs:
        await _collect_file(sid, file_segs[0], text, owner=_owner, meta=meta)
        _kick(sid)
        return

    rec_segs = [s for s in event.message if getattr(s, "type", "") == "record"]
    if rec_segs:
        await _collect_voice(sid, rec_segs[0], text, meta=meta)
        _kick(sid)
        return

    img_segs = [s for s in event.message if getattr(s, "type", "") == "image"]
    if img_segs:
        await _collect_images(sid, img_segs, text, meta=meta)
        _kick(sid)
        return

    if not text or text.startswith("/"):
        return

    _add(sid, text=text, meta=meta)
    _kick(sid)
