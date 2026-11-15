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

from app_core import asr, config, mood, paths, tts
from app_core.delivery import Capability
from app_core.runner import TurnRunner
from adapters.feishu import parse_content, to_inbound

# 和 QQ 共用同一个 ChatCore / Engine（chat 插件在模块导入时创建）
from plugins.chat import core, engine

driver = get_driver()

# 飞书文本消息上限远大于 QQ；这里给个保守值，超长由 CAP 截断
FEISHU_CAPS = Capability(name="feishu", max_text_chars=4000, max_parts=6,
                         supports_images=True)

_cfg = (config.load().get("feishu") or {})
ENABLED = bool(_cfg.get("enabled", False))
OWNER = str(_cfg.get("owner_open_id") or "").strip()
MAX_CHARS = int(_cfg.get("max_text_chars", 4000) or 4000)

_client: "lark.Client | None" = None
_loop: asyncio.AbstractEventLoop | None = None
_seen: set[str] = set()
_chat_of: dict[str, str] = {}
_SAVE = paths.DATA / "files"


def _creds() -> tuple[str, str]:
    """从 keys.json 的 feishu 字段读 app_id/app_secret（不进仓库）。"""
    try:
        data = json.loads(paths.KEYS.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return "", ""
    feishu = data.get("feishu") if isinstance(data.get("feishu"), dict) else {}
    return (str(feishu.get("app_id") or "").strip(),
            str(feishu.get("app_secret") or "").strip())


def _new_client(app_id: str, app_secret: str) -> lark.Client:
    return lark.Client.builder().app_id(app_id).app_secret(app_secret).build()


def _send_message(chat_id: str, msg_type: str, content: dict) -> bool:
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
    return True


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

    def __init__(self, session: str, chat_id: str) -> None:
        self.session = session
        self.chat_id = chat_id
        self.sent_text: list[str] = []

    async def send_part(self, session: str, part) -> bool:
        if part.form == "text":
            return await self.send_text(session, part.content)
        if part.form in ("image", "drawing"):
            return await self.send_image(session, part.content)
        if part.form == "voice":
            return await self.send_voice(session, part.content)
        return False

    async def send_text(self, session: str, text: str) -> bool:
        text = (text or "").strip()
        if not text:
            return False
        ok = await asyncio.to_thread(
            _send_message, self.chat_id, "text", {"text": text[:MAX_CHARS]})
        if ok:
            self.sent_text.append(text)
        return ok

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

    async def send_voice(self, session: str, path: str) -> bool:
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
            return await asyncio.to_thread(
                _send_message, self.chat_id, "audio",
                {"file_key": key, "duration": duration})
        finally:
            if own:
                opus.unlink(missing_ok=True)

    async def typing(self, session: str, on: bool) -> None:
        # 飞书机器人没有「正在输入」；保持空实现，编排层不用关心
        return None


async def _speak_later(chat_id: str, text: str) -> None:
    """文字发完后补一条语音（和 QQ 同一策略：只在他要/他发语音时才发）。"""
    try:
        say = (text or "").strip()[:300]
        if not say:
            return
        # 飞书直接吃 opus：走 tts.synth_opus（Breeze 直出，少一次 silk 转码）
        audio = await tts.synth_opus(say, mood=mood.load())
        if not audio:
            logger.info("[feishu] 语音合成返回空，跳过")
            return
        ch = FeishuChannel(session="", chat_id=chat_id)
        ok = await ch.send_voice("", str(audio))
        audio.unlink(missing_ok=True)
        logger.info(f"[feishu] voice sent={ok}")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[feishu] 语音链路失败：{e!r}")


async def _handle(data) -> None:
    """处理一条飞书事件（3 秒内返回由 _on_event 保证，这里是后台任务）。"""
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
        if mid and mid in _seen:
            return
        if mid:
            _seen.add(mid)
            if len(_seen) > 500:
                _seen.clear()

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

        session = config.owner_session() or f"feishu:{open_id}"
        _chat_of[session] = str(msg.chat_id)
        ch = FeishuChannel(session=session, chat_id=str(msg.chat_id))
        inbound = to_inbound(open_id=open_id, chat_id=str(msg.chat_id), text=text,
                             images=images, event_id=mid, voice=voice, session=session)
        runner = TurnRunner(engine, ch, caps=FEISHU_CAPS, deadline=core.reply_deadline)
        logger.info(f"[feishu] recv {session}: {(text or '')[:60]!r}"
                    f"{f'（{len(images)} 图）' if images else ''}")
        try:
            await runner.run(inbound)
        except asyncio.TimeoutError:
            logger.error(f"[feishu] 回复超时（>{core.reply_deadline:.0f}s） {session}")
            if ch.sent_text:
                await asyncio.to_thread(
                    _send_message, msg.chat_id, "text", {"text": "这次想太久了，先说到这。"})
        want_voice = voice or any(k in text for k in
                                  ("用语音", "语音说", "发语音", "读给我听", "念给我听"))
        if want_voice and ch.sent_text:
            asyncio.create_task(_speak_later(str(msg.chat_id), "".join(ch.sent_text)))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[feishu] 处理失败：{e!r}")


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
    _client = _new_client(app_id, app_secret)
    _loop = asyncio.get_running_loop()
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
    global _client
    _client = None
