"""feishu 插件：飞书长连接 → 内核（只和飞书主人私聊）。

为什么用长连接：飞书自建应用可以走 WebSocket 接收事件，不需要公网 IP / 域名 /
内网穿透；云端只要能出网就行。事件要在 3 秒内处理完，所以这里「立刻返回、
后台跑一轮」——真正的对话交给 app_core.runner（和 QQ 同一套 45 秒硬线/分段/锁）。

身份：只认 config.feishu.owner_open_id（可用范围设「仅自己」）。主人消息的
session 用 config.owner_session()，和 QQ 共用一份记忆——换端也记得住。
发送走 chat_id（p2p 会话 id，比再用 open_id 少一次映射）。

语音：飞书语音消息要求 msg_type=audio + 上传 file_type=opus；TTS 出的是 QQ 的
silk，所以这里 silk→pcm→opus 转一次。入站语音反过来：下载→ffmpeg 转 wav→ASR。
"""
from __future__ import annotations

import asyncio
import time
import json
import subprocess
import tempfile
import threading
from pathlib import Path

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateFileRequest,
    CreateFileRequestBody,
    CreateImageRequest,
    CreateImageRequestBody,
    CreateMessageRequest,
    CreateMessageRequestBody,
    GetMessageResourceRequest,
)
from nonebot import get_driver, logger

from app_core import asr, config, paths, tts
from app_core.delivery import BUFFERED, Capability, DeliveryResult, Receipt
from app_core.seen import BoundedSeen
from app_core.runner import TurnRunner, TurnTiming
from app_core.outbound import READ_ALOUD_DIRECTIVE
from adapters.feishu import parse_content, to_inbound

# 和 QQ 共用宿主启动的同一个应用。
from companion_core import shared_application

_application = shared_application()
core = _application.core
engine = _application.engine

driver = get_driver()

# 飞书文本消息上限远大于 QQ；这里给个保守值，超长由 CAP 截断
FEISHU_CAPS = Capability(name="feishu", max_text_chars=4000, max_parts=6,
                         supports_images=True)

_cfg = (config.load().get("feishu") or {})
ENABLED = bool(_cfg.get("enabled", False))
OWNER = str(_cfg.get("owner_open_id") or "").strip()
MAX_CHARS = int(_cfg.get("max_text_chars", 4000) or 4000)
# 语音回复形态（和 QQ 同一套配置）：旧行为 / 先语音再文字 / 只语音
_CHAT_CFG = (config.load().get("chat") or {})
_VMODE = str(_CHAT_CFG.get("voice_reply_mode") or "voice").strip().lower()
VOICE_REPLY_MODE = _VMODE if _VMODE in ("text_then_voice", "voice_then_text", "voice") else "voice"

_client: "lark.Client | None" = None
_loop: asyncio.AbstractEventLoop | None = None
# 有界去重：超过 500 条只淘汰最旧的，不再 clear() 连刚收到的 ID 一起清掉。
_seen = BoundedSeen(500)
_chat_of: dict[str, str] = {}
_SAVE = paths.DATA / "files"
# 主动消息要用 chat_id，但 `_chat_of` 以前只在内存里（重启就丢）。
# 落一份 open_id → chat_id，主人先跟 bot 说过话才能主动发。
_CHAT_MAP = paths.DATA / "feishu-chats.json"


def _load_chat_map() -> None:
    try:
        if _CHAT_MAP.exists():
            d = json.loads(_CHAT_MAP.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                _chat_of.update({str(k): str(v) for k, v in d.items()})
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[feishu] 会话映射读取失败：{e}")


def _save_chat_map() -> None:
    try:
        _CHAT_MAP.parent.mkdir(parents=True, exist_ok=True)
        tmp = _CHAT_MAP.with_suffix(".tmp")
        tmp.write_text(json.dumps(_chat_of, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        tmp.replace(_CHAT_MAP)
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[feishu] 会话映射保存失败：{e}")


async def _send_proactive(text: str):
    """主动给主人发一条飞书消息（#14）。没有会话映射就不硬猜 chat_id。

    成功要写回历史——和 QQ 的 `_send` 一样，否则下一轮它不知道自己主动说过话。
    """
    if not _client or not OWNER or not (text or "").strip():
        return False
    # 与入站共用同一个会话解析：QQ+飞书共用 private:<QQ> 时，主动消息必须
    # 写进那条会话，否则用户紧接着回复，模型读的历史里没有上一句。
    session = config.owner_session() or f"feishu:{OWNER}"
    chat_id = (_chat_of.get(session) or _chat_of.get(f"feishu:{OWNER}")
               or _chat_of.get(OWNER))
    if not chat_id:
        logger.warning("[feishu] 还没有会话映射，主动消息跳过（需主人先发一条消息）")
        return False
    ok = await asyncio.to_thread(
        _send_message, chat_id, "text", {"text": text})
    return Receipt("feishu", ok if isinstance(ok, str) else "") if ok else False


def _creds() -> tuple[str, str]:
    """从 keys.json 的 feishu 字段读 app_id/app_secret（不进仓库）。"""
    from app_core.kernel import keys

    feishu = keys.section("feishu")
    return (str(feishu.get("app_id") or "").strip(),
            str(feishu.get("app_secret") or "").strip())


def _new_client(app_id: str, app_secret: str) -> lark.Client:
    return lark.Client.builder().app_id(app_id).app_secret(app_secret).build()


def _send_message(chat_id: str, msg_type: str, content: dict) -> bool | str:
    body = (CreateMessageRequestBody.builder()
            .receive_id(chat_id)
            .msg_type(msg_type)
            .content(json.dumps(content, ensure_ascii=False))
            .build())
    req = (CreateMessageRequest.builder()
           .receive_id_type("chat_id")
           .request_body(body)
           .build())
    resp = _client.im.v1.message.create(req)
    if getattr(resp, "code", 0) != 0:
        logger.warning(f"[feishu] 发送失败 type={msg_type} code={getattr(resp, 'code', '?')} "
                       f"msg={getattr(resp, 'msg', '?')}")
        return False
    return str(getattr(getattr(resp, "data", None), "message_id", "") or "") or True


def _upload_image(path: Path) -> str:
    with path.open("rb") as f:
        body = (CreateImageRequestBody.builder()
                .image_type("message")
                .image(f)
                .build())
        req = CreateImageRequest.builder().request_body(body).build()
        resp = _client.im.v1.image.create(req)
    if getattr(resp, "code", 0) != 0 or not getattr(resp, "data", None):
        logger.warning(f"[feishu] 图片上传失败 code={getattr(resp, 'code', '?')} "
                       f"msg={getattr(resp, 'msg', '?')}")
        return ""
    return str(resp.data.image_key or "")


def _upload_file(path: Path, file_type: str, name: str, duration: int = 0) -> str:
    with path.open("rb") as f:
        b = (CreateFileRequestBody.builder()
             .file_type(file_type)
             .file_name(name)
             .duration(int(duration))
             .file(f)
             .build())
        req = CreateFileRequest.builder().request_body(b).build()
        resp = _client.im.v1.file.create(req)
    if getattr(resp, "code", 0) != 0 or not getattr(resp, "data", None):
        logger.warning(f"[feishu] 文件上传失败 type={file_type} "
                       f"code={getattr(resp, 'code', '?')} msg={getattr(resp, 'msg', '?')}")
        return ""
    return str(resp.data.file_key or "")


def _download_resource(message_id: str, file_key: str, kind: str) -> bytes:
    req = (GetMessageResourceRequest.builder()
           .message_id(message_id)
           .file_key(file_key)
           .type(kind)
           .build())
    resp = _client.im.v1.message_resource.get(req)
    data = getattr(resp, "file", None)
    if hasattr(data, "read"):
        data = data.read()
    return bytes(data or b"")


def _save_image(message_id: str, image_key: str) -> Path | None:
    data = _download_resource(message_id, image_key, "image")
    if not data:
        return None
    ext = ".jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        ext = ".png"
    elif data[:4] == b"GIF8":
        ext = ".gif"
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        ext = ".webp"
    _SAVE.mkdir(parents=True, exist_ok=True)
    out = _SAVE / f"feishu-{message_id[-12:]}-{image_key[-8:]}{ext}"
    out.write_bytes(data)
    return out


def _audio_duration_ms(path: Path) -> int:
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                              "format=duration", "-of", "csv=p=0", str(path)],
                             capture_output=True, text=True, timeout=30)
        return max(300, int(float(out.stdout.strip() or 0) * 1000))
    except Exception:  # noqa: BLE001
        return 1000


def _silk_to_opus(silk: Path) -> tuple[Path, int] | None:
    """QQ 的 silk → 飞书要的 opus；返回 (路径, 时长毫秒)。"""
    try:
        import pilk

        fd, pcm_name = tempfile.mkstemp(prefix="feishu-", suffix=".pcm")
        import os
        os.close(fd)
        pcm_path = Path(pcm_name)
        pilk.decode(str(silk), str(pcm_path), pcm_rate=24000)
        fd2, opus_name = tempfile.mkstemp(prefix="feishu-", suffix=".opus")
        os.close(fd2)
        opus_path = Path(opus_name)
        r = subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-f", "s16le", "-ar", "24000",
             "-ac", "1", "-i", str(pcm_path), "-c:a", "libopus", "-b:a", "24k",
             str(opus_path)], capture_output=True, timeout=60)
        pcm_bytes = pcm_path.stat().st_size if pcm_path.exists() else 0
        pcm_path.unlink(missing_ok=True)
        if r.returncode != 0 or not opus_path.exists() or opus_path.stat().st_size <= 0:
            opus_path.unlink(missing_ok=True)
            return None
        duration = int(pcm_bytes / 2 / 24000 * 1000)
        return opus_path, max(200, duration)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[feishu] silk→opus 失败：{e!r}")
        return None


def _audio_to_wav(message_id: str, file_key: str) -> Path | None:
    data = _download_resource(message_id, file_key, "file")
    if not data:
        return None
    fd, name = tempfile.mkstemp(prefix="feishu-", suffix=".opus")
    import os
    os.close(fd)
    raw = Path(name)
    raw.write_bytes(data)
    try:
        wav = asr.to_wav(raw)
        return wav
    finally:
        raw.unlink(missing_ok=True)


class FeishuChannel:
    """飞书通道：IO 原语 + 能力声明，会话循环交给 app_core.runner。"""

    forms = {"text", "image", "voice"}

    def __init__(self, session: str, chat_id: str, defer_text: bool = False) -> None:
        self.session = session
        self.chat_id = chat_id
        self.sent_text: list[str] = []
        self.defer_text = defer_text
        self.buffered: list[str] = []

    async def send_part(self, session: str, part) -> DeliveryResult:
        if part.form == "text":
            return await self.send_text(session, part.content)
        if part.form in ("image", "drawing"):
            return await self.send_image(session, part.content)
        if part.form == "voice":
            return await self.send_voice(session, part.content)
        return False

    async def send_text(self, session: str, text: str) -> DeliveryResult:
        text = (text or "").strip()
        if not text:
            return False
        if self.defer_text:
            # 已缓冲 ≠ 已送达，也 ≠ 发送失败。返回 BUFFERED：不要再补发，
            # 历史等语音/文字兜底真正成功后再写。
            self.buffered.append(text)
            return BUFFERED
        ok = await asyncio.to_thread(
            _send_message, self.chat_id, "text", {"text": text[:MAX_CHARS]})
        if not ok:
            return False
        sent = text[:MAX_CHARS]
        self.sent_text.append(sent)
        return Receipt("feishu", ok if isinstance(ok, str) else "", sent)

    async def send_image(self, session: str, path: str) -> bool:
        p = Path(path)
        if not p.exists():
            logger.warning(f"[feishu] 图片不存在：{p}")
            return False
        key = await asyncio.to_thread(_upload_image, p)
        if not key:
            return False
        return await asyncio.to_thread(
            _send_message, self.chat_id, "image", {"image_key": key})

    async def send_voice(self, session: str, path: str) -> bool | Receipt:
        p = Path(path)
        if not p.exists():
            logger.warning(f"[feishu] 语音不存在：{p}")
            return False
        own = False
        if p.suffix.lower() == ".opus":
            # Breeze 直出的 opus：不再解 silk、不再重编码（少一次有损转码）
            opus = p
            duration = await asyncio.to_thread(_audio_duration_ms, p)
        else:
            conv = await asyncio.to_thread(_silk_to_opus, p)
            if not conv:
                return False
            opus, duration = conv
            own = True
        try:
            key = await asyncio.to_thread(
                _upload_file, opus, "opus", "voice.opus", duration)
            if not key:
                return False
            result = await asyncio.to_thread(
                _send_message, self.chat_id, "audio",
                {"file_key": key, "duration": duration})
            return Receipt("feishu", result if isinstance(result, str) else "") if result else False
        finally:
            if own:
                opus.unlink(missing_ok=True)

    async def send_progress(self, session: str, text: str) -> bool:
        return bool(await asyncio.to_thread(_send_message, self.chat_id, "text", {"text": text}))

    async def typing(self, session: str, on: bool) -> None:
        # 飞书机器人没有「正在输入」；保持空实现，编排层不用关心
        return None


async def _speak_later(session: str, chat_id: str, text: str,
                       rewrite: bool = True) -> None:
    """文字发完后补一条语音。公共编排在 app_core/outbound.py。"""
    from app_core import outbound

    ch = FeishuChannel(session="", chat_id=chat_id)
    await outbound.speak_later(
        text=text,
        synth=lambda seg: tts.synth_opus(seg, mood=core.mood.load(), rewrite=rewrite),
        send_voice=lambda p: ch.send_voice("", p),
        send_text=lambda t: ch.send_text("", t),
        record=lambda text, kind: core.record_assistant(session, text, kind=kind),
        tag="[feishu]",
    )


async def _draw_later(session: str, chat_id: str, ask: str) -> None:
    """后台画图并发送。公共编排在 app_core/outbound.py。"""
    from app_core import image_provider, outbound

    ch = FeishuChannel(session=session, chat_id=chat_id)
    await outbound.draw_later(
        ask=ask,
        generate=image_provider.generate,
        cleanup=image_provider._cleanup,
        send_image=lambda p: ch.send_image("", p),
        send_text=lambda t: ch.send_text("", t),
        record=lambda text, kind: core.record_assistant(session, text, kind=kind),
        tag="[feishu]",
    )


async def _deliver_voice_reply(session: str, ch: FeishuChannel,
                                 rewrite: bool = True, timing: TurnTiming | None = None) -> None:
    """voice / voice_then_text 形态：先发语音，按配置决定是否再补文字。"""
    from app_core import outbound

    buffered = list(ch.buffered)
    ch.defer_text = False
    await outbound.deliver_voice_reply(
        buffered=buffered,
        synth=lambda seg: tts.synth_opus(seg, mood=core.mood.load(), rewrite=rewrite),
        send_voice=lambda p: ch.send_voice("", p),
        send_text=lambda t: ch.send_text("", t),
        record_delivery=lambda text, refs: core.record_delivery(session, text, references=refs),
        mode=VOICE_REPLY_MODE,
        tag="[feishu]",
        on_delivery=(lambda: timing.mark("first_sent")) if timing else None,
    )


async def _handle(data) -> None:
    """处理一条飞书事件（3 秒内返回由 _on_event 保证，这里是后台任务）。"""
    received = time.monotonic()
    timing = None
    try:
        event = data.event
        msg = event.message
        sender = event.sender
        if msg.chat_type != "p2p":
            logger.info(f"[feishu] 群消息忽略（chat_type={msg.chat_type}）")
            return
        open_id = str(getattr(sender.sender_id, "open_id", "") or "")
        if not OWNER or open_id != OWNER:
            logger.info(f"[feishu] 非主人消息忽略 open_id={open_id[:10]}")
            return
        mid = str(msg.message_id or "")
        if mid and not _seen.add(mid):
            return

        timing = TurnTiming("feishu", received=received)
        parsed = parse_content(msg.message_type, msg.content)
        text = str(parsed["text"] or "")
        images: list[str] = []
        for key in parsed["image_keys"]:
            p = await asyncio.to_thread(_save_image, mid, key)
            if p:
                images.append(str(p))
        voice = False
        if parsed["audio_key"]:
            wav = await asyncio.to_thread(_audio_to_wav, mid, parsed["audio_key"])
            res: dict = {}
            if wav:
                res = await asr.transcribe(wav)
                if res.get("text"):
                    text = (text + "\n" + str(res["text"])).strip()
                    voice = True
                wav.unlink(missing_ok=True)
            if not text:
                logger.info(f"[feishu] 语音转写为空：{str(res.get('error') or '')[:80]}")
        if voice and text:
            # 和 QQ 的历史格式保持一致：让模型知道「这句是语音转写」，
            # 否则它会说「我拿到手的分不出是打字还是转的」。入库也带这个标记。
            text = "[语音] " + text
        if not text and not images:
            await asyncio.to_thread(
                _send_message, msg.chat_id, "text",
                {"text": "这条消息我暂时读不了（%s）" % (parsed["unsupported"] or msg.message_type)})
            return

        # 主人消息的会话由 config 统一解析；映射同时记平台键，主动消息
        # 在共享会话和平台会话两种布局下都能找到 chat_id。
        session = config.owner_session() or f"feishu:{open_id}"
        try:
            core.note_user_active(session)
        except Exception:  # noqa: BLE001
            pass
        _chat_of[session] = str(msg.chat_id)
        _chat_of[f"feishu:{open_id}"] = str(msg.chat_id)
        # 主动消息要靠这份映射；落盘，重启后仍然找得到
        _save_chat_map()
        reply_to = getattr(msg, "parent_id", None)
        timing.mark("input_ready")
        timing.mark("intent_start")
        decision = await core.interpret_intent(
            session, text, voice=voice, reply_to=f"feishu:{reply_to}" if reply_to else None)
        timing.mark("intent_finish")
        timing.reply_form = "voice" if decision.voice else "text"
        model_text = READ_ALOUD_DIRECTIVE + text if decision.read_aloud else text
        ch = FeishuChannel(session=session, chat_id=str(msg.chat_id),
                           defer_text=decision.voice and VOICE_REPLY_MODE != "text_then_voice")
        inbound = to_inbound(open_id=open_id, chat_id=str(msg.chat_id),
                             text=model_text, images=images, event_id=mid,
                             voice=voice, session=session, store_text=text,
                             reply_to=reply_to, intent=decision)
        runner = TurnRunner(engine, ch, caps=FEISHU_CAPS, deadline=core.reply_deadline,
                            stall_seconds=getattr(core, "reply_stall_seconds", 30), timing=timing)
        logger.info(f"[feishu] recv {session}: {(text or '')[:60]!r}"
                    f"{f'（{len(images)} 图）' if images else ''}")
        try:
            out = await runner.run(inbound)
        except asyncio.TimeoutError:
            logger.error(f"[feishu] 回复收尾超时（总上限 {core.reply_deadline:.0f}s） {session}")
            if not ch.sent_text:
                _miss = "这次想太久了，先没说出来。"
                sent_ok = await asyncio.to_thread(
                    _send_message, msg.chat_id, "text", {"text": _miss})
                if sent_ok:
                    try:
                        await core.record_assistant(session, _miss, kind="text")
                    except Exception:  # noqa: BLE001
                        pass
            elif ch.sent_text:
                _tail = "这次想太久了，先说到这。"
                # 先发再记：发送失败时不能把这句写进历史。已发出的正文由
                # runner 在超时路径上按确认结果写回，这里只补收尾。
                sent_ok = await asyncio.to_thread(
                    _send_message, msg.chat_id, "text", {"text": _tail})
                if sent_ok:
                    try:
                        await core.record_assistant(session, _tail, kind="text")
                    except Exception:  # noqa: BLE001
                        pass
            # 超时时 out 未赋值，下面第 469 行原本还会读 getattr(out, "images")，
            # 会二次抛 UnboundLocalError；发图/语音/画图也不该在超时后继续。
            return
        if getattr(out, "images_sent", None):
            try:
                await core.record_assistant(
                    session, "（另外发了一张图）", kind="image")
            except Exception:  # noqa: BLE001
                pass
        if decision.voice and ch.buffered:
            await _deliver_voice_reply(session, ch, rewrite=not decision.read_aloud, timing=timing)
        elif decision.voice and ch.sent_text:
            asyncio.create_task(
                _speak_later(session, str(msg.chat_id), "".join(ch.sent_text),
                             rewrite=not decision.read_aloud))
        # 画画：只在他明确要求时画（和 QQ 同一策略）
        if decision.draw:
            asyncio.create_task(_draw_later(session, str(msg.chat_id), text))
    except Exception as e:  # noqa: BLE001
        if timing:
            timing.status = "error"
        logger.warning(f"[feishu] 处理失败：{e!r}")
    finally:
        if timing:
            timing.emit()


def _on_event(data) -> None:
    """长连接线程里的回调：立刻返回，处理丢给主事件循环（3 秒 ack）。"""
    if _loop is None:
        return
    asyncio.run_coroutine_threadsafe(_handle(data), _loop)


@driver.on_startup
async def _start() -> None:
    global _client, _loop
    if not ENABLED:
        logger.info("[feishu] 未启用（config.feishu.enabled=false）")
        return
    app_id, app_secret = _creds()
    if not app_id or not app_secret:
        logger.warning("[feishu] keys.json 缺 feishu.app_id/app_secret，跳过")
        return
    if not OWNER:
        logger.warning("[feishu] config.feishu.owner_open_id 未配置，跳过（只和主人聊）")
        return
    await _application.start(catch_up=True)
    if _client is not None:
        return
    _client = _new_client(app_id, app_secret)
    _loop = asyncio.get_running_loop()
    # 注册主动发送能力（注册 ≠ 启用，是否使用由 proactive.channel 决定）
    _load_chat_map()
    core.proactive_channels.register("feishu", _send_proactive)
    handler = (lark.EventDispatcherHandler.builder("", "")
               .register_p2_im_message_receive_v1(_on_event)
               .build())
    ws = lark.ws.Client(app_id, app_secret, event_handler=handler,
                        log_level=lark.LogLevel.INFO)
    threading.Thread(target=ws.start, name="feishu-ws", daemon=True).start()
    logger.info(f"[feishu] 长连接启动（owner={OWNER[:8]}…）")


@driver.on_shutdown
async def _stop() -> None:
    # 长连接线程是 daemon，进程退出即断；这里只清引用，避免热重载时重复启动
    global _client, _loop
    _loop = None
    _client = None
    core.proactive_channels.unregister("feishu")
