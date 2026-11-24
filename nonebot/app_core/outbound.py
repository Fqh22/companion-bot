"""[orchestration] 语音 / 画图 / 朗读的公共编排：通道只提供发送回调。

为什么上移（2026-11-16）：QQ 与飞书两个插件各写了一遍 —— _speak_later 44/31 行、
_draw_later 33/30 行、_deliver_voice_reply 32/31 行、_read_aloud_requested 完全重复。
两版真正的差异只有两处：TTS 格式（silk / opus）和发送方法。接第三个通道不该再抄一遍。

契约：
- synth(seg) -> Path | None：合成一段音频（QQ 传 tts.synth，飞书传 tts.synth_opus）。
- send_voice(path) -> bool / send_image(path) -> bool / send_text(text)。
- record(text, kind)：写回历史的旁注（"这句话我也用语音说了一遍"）。
- 失败不抛：文字已经发出去了，语音/图是补充不是替代；失败要补一句并留痕。
"""
from __future__ import annotations

import logging
from typing import Awaitable, Callable

from app_core import pacing
from app_core.delivery import Receipt, is_buffered

log = logging.getLogger("qqbot.outbound")

READ_ALOUD_KEYS = ("读一遍", "念一遍", "读一下", "念一下", "朗读", "照着读",
                   "照着念", "原样念", "一字不差")
READ_ALOUD_DIRECTIVE = ("【朗读模式】用户要求把下面这段文字原样念出来："
                        "逐字输出原文，不要改写、不要寒暄、不要加解释。\n\n")
DRAW_TRIGGERS = ("画一张", "画个", "画幅", "画一下", "帮我画", "给我画")

Synth = Callable[[str], Awaitable[object]]
Sender = Callable[[str], Awaitable[bool | Receipt]]
TextSender = Callable[[str], Awaitable[object]]
Recorder = Callable[[str, str], Awaitable[object]]
DeliveryRecorder = Callable[[str, tuple[tuple[str, str], ...]], Awaitable[object]]


def read_aloud_requested(text: str) -> bool:
    return any(k in (text or "") for k in READ_ALOUD_KEYS)


async def _safe_record(record: Recorder | None, text: str, kind: str) -> None:
    if record is None:
        return
    try:
        await record(text, kind)
    except Exception as e:  # noqa: BLE001
        log.debug("旁注写回失败：%s", e)


async def speak_later(*, text: str, synth: Synth, send_voice: Sender,
                      send_text: TextSender, record: Recorder | None = None,
                      tag: str = "") -> bool:
    """文字发完后补语音：按句切段逐段合成发送；失败补一句文字。

    与旧实现一致：只发语音、不重复文字；单段时 split_for_voice 原样返回，
    行为逐字节不变。
    """
    say = (text or "").strip()
    segments = pacing.split_for_voice(say)
    if not segments:
        return False
    ok = False
    for seg in segments:
        audio = await synth(seg)
        if not audio:
            continue
        try:
            if await send_voice(str(audio)):
                ok = True
        finally:
            try:
                audio.unlink(missing_ok=True)  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                pass
    log.info("%s voice sent=%s %s 字 / %s 段", tag, ok, len(say), len(segments))
    if not ok:
        try:
            await send_text("（语音没发出去，文字顶上）")
        except Exception:  # noqa: BLE001
            pass
        await _safe_record(record, "（语音没发出去，我补了一句文字）", "voice_fail")
    else:
        await _safe_record(record, "（这句话我也用语音说了一遍）", "voice")
    return ok


async def draw_later(*, ask: str, generate, cleanup, send_image: Sender,
                     send_text: TextSender, record: Recorder | None = None,
                     tag: str = "") -> bool:
    """后台画图并发送；画不出来补一句，不打断主回复。"""
    try:
        path = await generate(ask)
        if not path:
            try:
                await send_text("这次没画出来，等下再试。")
            except Exception:  # noqa: BLE001
                pass
            await _safe_record(record, "（这次画图失败了，我跟他说明了）", "draw_fail")
            return False
        try:
            cleanup(path)
        except Exception:  # noqa: BLE001
            pass
        ok = await send_image(str(path))
        log.info("%s drawing sent=%s", tag, ok)
        if ok:
            await _safe_record(record, f"（我刚画了一张图发给他了：{ask[:40]}）", "draw")
        return ok
    except Exception as e:  # noqa: BLE001
        log.warning("%s 画图链路失败：%s: %s", tag, type(e).__name__, e)
        return False


async def deliver_voice_reply(*, buffered: list[str], synth: Synth, send_voice: Sender,
                              send_text: TextSender, record: Recorder | None = None,
                              mode: str = "voice", tag: str = "",
                              record_delivery: DeliveryRecorder | None = None) -> bool:
    """voice / voice_then_text 形态：先语音，按 mode 决定要不要再补文字。

    这些文本在通道层只是**缓冲**（没有投递），所以历史必须等这里真正发出
    语音或文字兜底后再写；两边都失败就一个字都不写。写的是实际说出口的
    正文，不是「我用语音说了一遍」这种旁注。
    """
    say = "".join(buffered).strip()
    if not say:
        return False
    segments = list(pacing.split_for_voice(say))
    references: list[tuple[str, str]] = []
    voiced: list[str] = []
    failed: list[str] = []
    # 分段有上限。超出的尾巴不能静默丢掉，也不能因为前面发成功就报整段成功。
    cursor = 0
    for seg in segments:
        i = say.find(seg, cursor)
        if i < 0:
            break
        cursor = i + len(seg)
    remainder = say[cursor:].strip()
    try:
        for seg in segments:
            audio = await synth(seg)
            sent = False
            if audio:
                try:
                    result = await send_voice(str(audio))
                    sent = bool(result)
                    if isinstance(result, Receipt) and result.reference:
                        references.append((result.reference, seg))
                finally:
                    try:
                        audio.unlink(missing_ok=True)  # type: ignore[attr-defined]
                    except Exception:  # noqa: BLE001
                        pass
            if sent:
                voiced.append(seg)
            else:
                failed.append(seg)
    except Exception as e:  # noqa: BLE001
        log.warning("%s 语音发送失败，退回文字：%s", tag, e)
        seen = len(voiced) + len(failed)
        failed.extend(segments[seen:])
    if remainder:
        failed.append(remainder)

    text_ok: list[str] = []
    text_bad: list[str] = []

    async def _send_text_parts(parts: list[str]) -> None:
        for seg in parts:
            try:
                result = await send_text(seg)
                if result and not is_buffered(result):
                    delivered = result.text if isinstance(result, Receipt) and result.text is not None else seg
                    text_ok.append(delivered)
                    if isinstance(result, Receipt) and result.reference:
                        references.append((result.reference, delivered))
                else:
                    text_bad.append(seg)
            except Exception:  # noqa: BLE001
                text_bad.append(seg)

    # 有一段语音成功就不再把全文重发一遍：失败段单独补文字。
    # 历史只记实际送达的内容，没送到的结尾不能让下一轮以为用户听到了。
    if mode == "voice_then_text":
        await _send_text_parts(list(buffered))
        if text_ok and not text_bad:
            spoken = "".join(text_ok).strip()
            ok = True
        else:
            spoken = "".join(text_ok).strip() or "".join(voiced)
            ok = bool(spoken) and not text_bad and not failed
    elif not voiced:
        await _send_text_parts(list(buffered))
        spoken = "".join(text_ok).strip()
        ok = bool(spoken) and not text_bad
    elif failed:
        await _send_text_parts(list(failed))
        spoken = "".join(voiced + text_ok)
        ok = bool(spoken) and not text_bad
    else:
        spoken = "".join(voiced)
        ok = bool(spoken)
    if spoken and record_delivery is not None:
        try:
            await record_delivery(spoken, tuple(references))
        except Exception as exc:
            log.warning("%s 语音投递入账失败：%s", tag, exc)
    elif spoken:
        await _safe_record(record, spoken, "voice")
    else:
        log.warning("%s 语音与文字兜底都没发出去，不写历史", tag)
    log.info("%s voice_reply mode=%s voice=%s text=%s ok=%s",
             tag, mode, len(voiced), len(text_ok), ok)
    return ok
