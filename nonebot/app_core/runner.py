"""[orchestration] 私聊入口与平台无关的回复投递。

InboundTurns 聚合文本和附件。私聊入口补充意图、记忆正文及回复形态，
TurnRunner 按会话串行执行引擎和投递，管理 typing、节奏、总时限与无进展超时。
通道通过 Channel 返回送达回执、缓冲状态或失败。
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import replace
from datetime import datetime
from typing import Protocol

from app_core import mood, pacing
from app_core.delivery import (BUFFERED, DELIVERY_UNKNOWN, Capability, ConfirmedText,
                               DeliveryPolicy, DeliveryResult, Receipt, is_buffered, is_delivered)
from app_core.engine import Engine, InboundMessage, OutboundMessage
from app_core.message import Part as _Part
from app_core.outbound import READ_ALOUD_DIRECTIVE, deliver_voice_reply
from app_core.round_timing import TurnTiming, current
from app_core.turn import IMAGE_UNAVAILABLE, Turn

log = logging.getLogger("qqbot.runner")


class Channel(Protocol):
    """一个通道 = 一份能力声明 + 一个 send。

    forms 声明这个平台支持哪些形式（"text" / "image" / "voice" / "drawing" / "file"）。
    runner 只按 Part.form 调 send；平台不支持的形式由 runner 侧降级，
    所以加一个平台只需要写 adapter，不需要改业务代码。
    """

    forms: set[str]

    async def send_part(self, session: str, part) -> DeliveryResult: ...

    async def typing(self, session: str, on: bool) -> None: ...


class PrivateChannel(Channel, Protocol):
    defer_text: bool
    buffered: list[str]
    sent_text: list[str]

    async def send_text(self, session: str, text: str) -> DeliveryResult: ...


# 同一会话串行：两轮并发会各自 prepare，互相覆盖记忆/情绪/跟催状态。
# 并发消息在真人聊天里也是「一句一句回」，排队是更对的行为，不是延迟缺陷。
_SESSION_LOCKS: dict[str, asyncio.Lock] = {}
_TYPING_TASKS: dict[tuple[type, str], asyncio.Task] = {}
_ACTIVE_RUNS: dict[str, dict] = {}


def request_supersede(session: str) -> bool:
    """若同会话上一轮尚未向用户发出任何片段且未执行副作用工具，取消它让新消息合并重跑。"""
    active = _ACTIVE_RUNS.get(session)
    if not active:
        return False
    state = active["state"]
    timing = active["timing"]
    if state.get("sent") or state.get("sending") or "side_effect" in timing.marks:
        return False
    handle = active.get("handle")
    if handle is None or handle.done():
        return False
    state["why"] = "superseded"
    handle.cancel()
    log.info("[runner] 新消息到达，抢占未发出的旧轮次：%s", session)
    return True


def voice_reply_mode(settings: dict) -> str:
    mode = str(settings.get("voice_reply_mode") or "voice").strip().lower()
    return mode if mode in ("text_then_voice", "voice_then_text", "voice") else "voice"


async def run_private_turn(turn: Turn, inbound: InboundMessage, channel: PrivateChannel, *,
                           engine: Engine, caps: Capability, voice_mode: str,
                           synthesize, speak, draw, notify, on_timeout,
                           store_composed: bool = False) -> None:
    """编排两端共用的私聊回合，通道负责平台字段、发送与超时提示。"""
    core = engine.core
    session = inbound.session
    platform = inbound.platform
    timing = TurnTiming(platform, received=turn.meta.get("received_at", time.monotonic()))
    timing.input_count = turn.meta["input_count"]
    timing.mark("batch_start")
    timing.marks["input_ready"] = turn.meta.get("ready_at", timing.received)
    text, stored = turn.compose()
    raw = "\n".join(turn.texts)
    log.info("[%s] recv %s: %r (%d images)", platform, session, text[:60], len(inbound.images))
    try:
        voice_failed = turn.meta.get("voice_unavailable")
        image_failed = turn.meta.get("image_unavailable")
        if turn.empty() and (image_failed or voice_failed):
            timing.status = "voice_unavailable" if voice_failed else "image_unavailable"
            message = "这段语音没转出来，可以重发或打字补一下。" if voice_failed else IMAGE_UNAVAILABLE
            if is_delivered(await notify(message)):
                timing.mark("first_sent")
            return

        timing.mark("intent_start")
        decision = await core.interpret_intent(
            session, raw, voice=inbound.voice, quote_text=inbound.quote_text,
            reply_to=inbound.reply_reference)
        timing.mark("intent_finish")
        timing.reply_form = "voice" if decision.voice else "text"
        channel.defer_text = decision.voice and voice_mode != "text_then_voice"
        prompt = turn.image_status(text)
        if decision.read_aloud:
            prompt = READ_ALOUD_DIRECTIVE + prompt
        if store_composed or (image_failed and stored is None):
            stored = text
        msg = replace(inbound, text=prompt, store_text=stored, intent=decision)
        request_supersede(session)
        runner = TurnRunner(engine, channel, caps=caps, deadline=core.reply_deadline,
                            stall_seconds=getattr(core, "reply_stall_seconds", 30), timing=timing)
        out = await runner.run(msg)
        if timing.status == "superseded":
            return
        if getattr(out, "images_sent", None):
            try:
                await core.record_assistant(session, "（另外发了一张图）", kind="image")
            except Exception:
                log.warning("[%s] 写回发图记录失败", platform, exc_info=True)
        rewrite = not decision.read_aloud
        if decision.voice and channel.buffered:
            channel.defer_text = False
            await deliver_voice_reply(
                buffered=list(channel.buffered),
                synth=lambda seg: synthesize(seg, mood=core.mood.load(), rewrite=rewrite),
                send_voice=lambda path: channel.send_part(session, _Part("voice", path)),
                send_text=lambda part: channel.send_text(session, part),
                record_delivery=lambda part, refs: core.record_delivery(session, part, references=refs),
                mode=voice_mode, tag=f"[{platform}]",
                on_delivery=lambda: timing.mark("first_sent"))
        elif decision.voice and channel.sent_text:
            asyncio.create_task(speak("".join(channel.sent_text), rewrite=rewrite))
        if decision.draw:
            asyncio.create_task(draw(text if store_composed else raw))
    except asyncio.TimeoutError:
        timing.status = "timeout"
        log.warning("[%s] 回复超时 %s，已发 %d 段", platform, session, len(channel.sent_text))
        await on_timeout(bool(channel.sent_text))
    except asyncio.CancelledError:
        timing.status = "cancelled"
        raise
    except Exception:
        timing.status = "error"
        raise
    finally:
        timing.emit()


async def send_delivery(core, session: str, text: str, send) -> None:
    """即时提示送达后再写回，保留后续引用所用的平台消息 ID。"""
    confirmed = ConfirmedText()
    if confirmed.add(text, await send(text)):
        await core.record_delivery(session, confirmed.parts[0],
                                   references=tuple(confirmed.references), track_followup=False)


class TurnRunner:
    """跑完一轮：typing 心跳 + 引擎 + 流式发送 + 补发尾巴。"""

    PROGRESS_AFTER = 3.0

    def __init__(self, engine: Engine, channel: Channel,
                 caps: Capability | None = None,
                 policy: DeliveryPolicy | None = None,
                 deadline: float = 90.0,
                 stall_seconds: float = 30.0,
                 typing_interval: float = 5.0,
                 timing: TurnTiming | None = None) -> None:
        self.engine = engine
        self.channel = channel
        self.caps = caps
        self.policy = policy
        # 宽松总上限：只防「永远不结束」，不是体验目标
        self.deadline = max(1.0, float(deadline))
        # 无进展超时：首句发出后多久没有新句子就收尾（取证阶段不受它约束）
        self.stall_seconds = max(1.0, float(stall_seconds))
        self.typing_interval = float(typing_interval)
        self.timing = timing

    def _remaining(self, started: float) -> float:
        """离整轮总上限还剩多少秒；用到 0 就停止一切平台调用。"""
        return max(0.0, self.deadline - (time.monotonic() - started))

    async def _send_part(self, session: str, part, timeout: float | None = None) -> DeliveryResult:
        """按形式发送；平台不支持的形式在这里降级。

        降级放在 runner 而不是 adapter：adapter 只回答「我会什么」，
        「不会的时候退成什么」是编排层的决定，换平台不用重复写。
        `timeout` 是硬上限：平台 API 挂住时，这一轮必须能收尾、锁必须释放，
        下一轮才能继续。
        """
        forms = getattr(self.channel, "forms", None) or {"text", "image"}
        if part.form in forms:
            try:
                if timeout is None:
                    result = await self.channel.send_part(session, part)
                else:
                    if timeout <= 0:
                        return False
                    result = await asyncio.wait_for(
                        self.channel.send_part(session, part), timeout=timeout)
            except asyncio.TimeoutError:
                log.warning("[runner] 发送 %s 超时（>%.1fs）", part.form, timeout or 0)
                return DELIVERY_UNKNOWN
            except Exception as e:  # noqa: BLE001
                log.warning("[runner] 发送 %s 失败：%s", part.form, e)
                return DELIVERY_UNKNOWN
            # 已缓冲 ≠ 已送达，也不能当成失败再发一遍。
            if result is DELIVERY_UNKNOWN or is_buffered(result):
                return result
            ok = bool(result)
            if not ok and part.form == "voice":
                # 声明支持 != 发得出去：合成/上传失败时也要有文字兜底
                return await self._send_part(
                    session, _Part("text", "（这条本来是语音，这里发不了）"), timeout)
            return result if isinstance(result, Receipt) else ok
        if part.form == "voice":
            # 语音发不了就说一句，不要静默丢——用户会以为漏回了
            return await self._send_part(
                session, _Part("text", "（这条本来是语音，这里发不了）"), timeout)
        if part.form == "drawing" and "image" in forms:
            # 不支持「生成图」的通道就退化成普通图片（docs/02-系统与扩展.md 的降级规则）
            return await self._send_part(session, _Part("image", part.content), timeout)
        log.info("[runner] 通道 %s 不支持 %s，已跳过", getattr(self.channel, "caps", "?"), part.form)
        return False

    async def _typing_safe(self, session: str, on: bool,
                           timeout: float = 5.0) -> None:
        """typing 是锦上添花：平台卡住时不能把它变成整轮的阻塞点。"""
        try:
            await asyncio.wait_for(self.channel.typing(session, on), timeout=timeout)
        except asyncio.TimeoutError:
            log.debug("[runner] typing(%s) 超时", on)
        except Exception as e:  # noqa: BLE001
            log.debug("[runner] typing(%s) 失败：%s", on, e)

    async def _typing_loop(self, session: str, previous: asyncio.Task | None) -> None:
        if previous is not None and not previous.done():
            await asyncio.shield(asyncio.gather(previous, return_exceptions=True))
        try:
            while True:
                await self._typing_safe(session, True)
                await asyncio.sleep(self.typing_interval)
        finally:
            await self._typing_safe(session, False, timeout=1.0)

    async def _pace_and_send(self, session: str, text: str, sent: int,
                             started: float) -> DeliveryResult:
        """发出这一条之前等「打字」时间；预算见底就跳过等待。"""
        left = self.deadline - (time.monotonic() - started)
        if left <= 0:
            return False
        # 首段立即投递。语音先缓冲，正文产生期间没有可见打字节奏。
        if sent == 0 or getattr(self.channel, "defer_text", False):
            return await self._send_part(session, _Part("text", text), timeout=left)
        try:
            m = mood.settle(mood.load())
        except Exception:  # noqa: BLE001
            m = {}
        gap = pacing.gap_seconds(sent) if sent else 0.0
        pace = pacing.pace_seconds(text, datetime.now().hour, m,
                                   fast=pacing.is_fast(sent) or left < 12)
        wait = gap + pace
        if time.monotonic() - started >= 2.5:
            wait = min(wait * 0.4, 1.0)
        wait = min(wait, max(0.0, left - 4))    # 留 4 秒给发送和收尾
        if wait > 0:
            await asyncio.sleep(wait)
        # 发送本身也吃时限：平台 API 挂住时，到点就放弃，别把会话锁拖死。
        return await self._send_part(
            session, _Part("text", text),
            timeout=self._remaining(started))

    async def _watchdog(self, handle: asyncio.Task, started: float,
                        state: dict) -> None:
        """总上限 + 无进展超时；触发时取消整轮，由 `_run` 转成 TimeoutError。

        取证/首字阶段（`state["sent"] == 0`）只受总上限约束：thinking 和工具
        本来就可能慢，用「多久没出句子」去掐它等于惩罚正常回复。
        发出第一句之后才算「生成在进行中」，这时久无进展就是真卡住了。
        """
        while not handle.done():
            now = time.monotonic()
            cap_left = self.deadline - (now - started)
            stall_left = (self.stall_seconds - (now - state["last_progress"])
                          if state["sent"] else float("inf"))
            wait = min(cap_left, stall_left)
            if wait <= 0:
                state["why"] = "stall" if stall_left <= cap_left else "cap"
                log.warning("[runner] 收尾：%s（已发 %d 段，%.1fs）",
                            state["why"], state["sent"], now - started)
                handle.cancel()
                return
            # 短 tick：deadline 很小时（测试里 0.2s）也要及时醒来
            await asyncio.sleep(min(wait, 0.25))

    async def run(self, msg: InboundMessage) -> OutboundMessage:
        """同一会话串行。计时从平台收件或宿主调用开始。"""
        timing = self.timing or TurnTiming(msg.platform)
        timing.mark("runner_queued")
        token = current.set(timing)
        try:
            lock = _SESSION_LOCKS.setdefault(msg.session, asyncio.Lock())
            async with lock:
                timing.mark("runner_start")
                return await self._run(msg, timing)
        except asyncio.TimeoutError:
            timing.status = "timeout"
            raise
        except asyncio.CancelledError:
            timing.status = "cancelled"
            raise
        except Exception:
            timing.status = "error"
            raise
        finally:
            current.reset(token)
            if self.timing is None:
                timing.emit()

    async def _remember_confirmed(self, msg: InboundMessage,
                                  parts: list[str], references: list[tuple[str, str]]) -> None:
        """只把通道确认发出去的文本写回历史。

        以前 commit 先入库、runner 后发送：发送失败时历史里也留着用户根本
        没收到的话（流式尾段 / 非流式回复 / 超时补发都踩过）。现在历史由这里
        统一写，parts 里每一段都是 on_sentence/_pace_and_send 返回成功的。
        """
        text = "".join(p for p in (parts or []) if p)
        if not text.strip():
            return
        core = getattr(self.engine, "core", None)
        record = getattr(core, "record_delivery", None)
        try:
            if record is not None:
                await asyncio.shield(record(msg.session, text, references=tuple(references)))
            elif remember := getattr(core, "remember_safe", None):
                # 兼容只实现历史接口的旧宿主。
                await asyncio.shield(remember(msg.session, "assistant", text))
        except Exception as e:  # noqa: BLE001
            log.warning("[runner] 写回已发送回复失败：%s", e)

    async def _run(self, msg: InboundMessage, timing: TurnTiming) -> OutboundMessage:
        started = time.monotonic()
        delivery = ConfirmedText()
        confirmed = delivery.parts
        state = {"last_progress": started, "sent": 0, "sending": False, "why": ""}
        pending: asyncio.Queue[tuple[str, bool] | None] = asyncio.Queue(maxsize=4)
        typing_key = (type(self.channel), msg.session)
        previous = _TYPING_TASKS.get(typing_key)
        typing = asyncio.create_task(self._typing_loop(msg.session, previous))
        _TYPING_TASKS[typing_key] = typing

        def forget_typing(task):
            if _TYPING_TASKS.get(typing_key) is task:
                _TYPING_TASKS.pop(typing_key, None)

        typing.add_done_callback(forget_typing)

        async def sender() -> None:
            interrupted = False
            while (entry := await pending.get()) is not None:
                part, progress = entry
                if progress:
                    if not confirmed and timing.checking:
                        timeout = min(2.0, self._remaining(started))
                        send_progress = getattr(self.channel, "send_progress", None)
                        try:
                            sent = (await asyncio.wait_for(send_progress(msg.session, part), timeout)
                                    if send_progress else
                                    await self._send_part(msg.session, _Part("text", part), timeout=timeout))
                        except Exception:
                            sent = False
                        if sent and not is_buffered(sent):
                            timing.mark("progress_sent")
                    continue
                if interrupted:
                    continue
                state["sending"] = True
                status = await self._pace_and_send(msg.session, part, len(confirmed), started)
                if status is False and self._remaining(started) > 0:
                    status = await self._send_part(
                        msg.session, _Part("text", part), timeout=self._remaining(started))
                if is_buffered(status):
                    continue
                if not status:
                    interrupted = True
                    timing.status = "delivery_unknown" if status is DELIVERY_UNKNOWN else "delivery_failed"
                    part = "刚才的回复发送中断了，我可以接着说。"
                    status = await self._send_part(
                        msg.session, _Part("text", part), timeout=min(2.0, self._remaining(started)))
                    if not is_delivered(status):
                        continue
                if not delivery.add(part, status):
                    continue
                state["sent"] = len(confirmed)
                state["last_progress"] = time.monotonic()
                timing.mark("first_sent")

        async def enqueue(part: str) -> DeliveryResult:
            state["sending"] = True
            state["last_progress"] = time.monotonic()
            await asyncio.wait_for(pending.put((part, False)), timeout=self._remaining(started))
            # 内核只知道片段已入队，发送结果由消费者统一回写。
            return BUFFERED

        async def notify_progress() -> None:
            await asyncio.sleep(self.PROGRESS_AFTER)
            while not confirmed and timing.reply_form != "voice" and not getattr(self.channel, "defer_text", False):
                if timing.checking and pending.empty():
                    pending.put_nowait(("我查一下。", True))
                    return
                await asyncio.sleep(0.25)

        worker = asyncio.create_task(sender())
        progress = asyncio.create_task(notify_progress())
        handle = asyncio.create_task(
            self.engine.handle(msg, on_sentence=enqueue, caps=self.caps,
                               delivery_policy=self.policy, defer_history=True))
        _ACTIVE_RUNS[msg.session] = {"state": state, "timing": timing, "handle": handle}
        watch = asyncio.create_task(self._watchdog(handle, started, state))
        try:
            try:
                out = await handle
            except asyncio.CancelledError:
                if state["why"] == "superseded":
                    timing.status = "superseded"
                    return OutboundMessage(text="", chunks=[])
                if state["why"]:
                    raise asyncio.TimeoutError() from None
                raise
            progress.cancel()
            for part in out.chunks:
                await enqueue(part)
            await asyncio.wait_for(pending.put(None), timeout=self._remaining(started))
            await asyncio.wait_for(worker, timeout=self._remaining(started))
            if timing.status in ("delivery_failed", "delivery_unknown"):
                return out
            for target, form, sent in ((out.images, "image", out.images_sent),
                                       (out.audio, "voice", out.audio_sent)):
                for path in target:
                    if await self._send_part(msg.session, _Part(form, path),
                                             timeout=self._remaining(started)):
                        sent.append(path)
                        timing.mark("first_sent")
            return out
        finally:
            if _ACTIVE_RUNS.get(msg.session, {}).get("handle") is handle:
                _ACTIVE_RUNS.pop(msg.session, None)
            for task in (watch, worker, progress, typing):
                task.cancel()
            await asyncio.gather(watch, worker, progress, return_exceptions=True)
            await self._remember_confirmed(msg, confirmed, delivery.references)
