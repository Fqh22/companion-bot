"""[orchestration] 通道无关的一轮对话：typing、流式、时限、分段节奏。

为什么要这一层：现在这套「锁 → 取回合 → 调引擎 → 边生成边发 → 补发尾巴」
写在 plugins/chat 里，换通道就得重写 300 行。这里把它抽出来，通道只实现
Channel 的三个原语：

    send_text(session, text) -> bool | "buffered"
        True 已送达；"buffered" 已收下但还没投递（语音缓冲，不要再发、先别写历史）；
        False 发送失败。
    send_image(session, path) -> bool    发一张图
    typing(session, on) -> None          显示/关闭「正在输入」（不支持就空实现）

引擎只认 InboundMessage/OutboundMessage，所以新通道不需要 NoneBot、不需要 QQ。

会话锁：原设计说「留给入站侧的 TurnQueue」，但 TurnQueue 至今只有定义、没有使用点
（2026-11-15 确认），并发轮次会各自 prepare 并互相覆盖记忆/情绪/跟催状态。
所以锁放在这里——宁可违背这条注释，也不要留一个只存在于文档里的保障。
typing 心跳与时限也在这里。

时限不再是一把尺子（2026-11-16）：
- `deadline` 是宽松总上限，只兜底，不决定「正常回复该多长」；
- `stall_seconds` 是无进展超时：发出第一句之后，久无新句子就收尾。
原来的 `wait_for(timeout=45)` 会用「取消整轮」把长回复/多段语音一刀切成半截，
而 45 这个数字是从「工具 6 轮最坏 9 分钟」倒推的，反过来绑架了正常路径。
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Protocol

from app_core import mood, pacing
from app_core.delivery import BUFFERED, Capability, DeliveryPolicy, DeliveryResult, Receipt, is_buffered
from app_core.engine import Engine, InboundMessage, OutboundMessage
from app_core.message import Part as _Part
from app_core.round_timing import TurnTiming, current

log = logging.getLogger("app_core.runner")


class Channel(Protocol):
    """一个通道 = 一份能力声明 + 一个 send。

    forms 声明这个平台支持哪些形式（"text" / "image" / "voice" / "drawing" / "file"）。
    runner 只按 Part.form 调 send；平台不支持的形式由 runner 侧降级，
    所以加一个平台只需要写 adapter，不需要改业务代码。
    """

    forms: set[str]

    async def send_part(self, session: str, part) -> DeliveryResult: ...

    async def typing(self, session: str, on: bool) -> None: ...


# 同一会话串行：两轮并发会各自 prepare，互相覆盖记忆/情绪/跟催状态。
# 并发消息在真人聊天里也是「一句一句回」，排队是更对的行为，不是延迟缺陷。
_SESSION_LOCKS: dict[str, asyncio.Lock] = {}
_TYPING_TASKS: dict[tuple[type, str], asyncio.Task] = {}


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
                result = False
            except Exception as e:  # noqa: BLE001
                log.warning("[runner] 发送 %s 失败：%s", part.form, e)
                result = False
            # 已缓冲 ≠ 已送达，也不能当成失败再发一遍。
            if is_buffered(result):
                return BUFFERED
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
        wait = min(gap + pace, max(0.0, left - 4))    # 留 4 秒给发送和收尾
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
        references: list[tuple[str, str]] = []
        confirmed: list[str] = []
        state = {"last_progress": started, "sent": 0, "why": ""}
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
                status = await self._pace_and_send(msg.session, part, len(confirmed), started)
                if not status or is_buffered(status):
                    continue
                text = status.text if isinstance(status, Receipt) and status.text is not None else part
                confirmed.append(text)
                if isinstance(status, Receipt) and status.reference:
                    references.append((status.reference, text))
                state["sent"] = len(confirmed)
                state["last_progress"] = time.monotonic()
                timing.mark("first_sent")

        async def enqueue(part: str) -> DeliveryResult:
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
        watch = asyncio.create_task(self._watchdog(handle, started, state))
        try:
            try:
                out = await handle
            except asyncio.CancelledError:
                if state["why"]:
                    raise asyncio.TimeoutError() from None
                raise
            progress.cancel()
            for part in out.chunks:
                await enqueue(part)
            await asyncio.wait_for(pending.put(None), timeout=self._remaining(started))
            await asyncio.wait_for(worker, timeout=self._remaining(started))
            for target, form, sent in ((out.images, "image", out.images_sent),
                                       (out.audio, "voice", out.audio_sent)):
                for path in target:
                    if await self._send_part(msg.session, _Part(form, path),
                                             timeout=self._remaining(started)):
                        sent.append(path)
                        timing.mark("first_sent")
            return out
        finally:
            for task in (watch, worker, progress, typing):
                task.cancel()
            await asyncio.gather(watch, worker, progress, return_exceptions=True)
            await self._remember_confirmed(msg, confirmed, references)
