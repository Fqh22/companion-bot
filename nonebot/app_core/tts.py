"""[infra] 语音合成：provider 可切换（智谱 cogtts / DeepSeek），失败自动回退。

链路：text --(provider /audio/speech)--> PCM 24k mono
      --(pilk.encode)--> silk（#!SILK_V，QQ 能直接发）
飞书通道再 silk→opus 发语音气泡。

为什么可切换：2026-11-15 用户充了智谱的钱（之前为「去单点」换到 DeepSeek）；
现在把智谱设为主、DeepSeek 设兜底——单点问题靠「两家互为 fallback」解决，
不靠二选一。哪家失败就换另一家，不抛异常：一条语音合成失败不该让整轮回复崩。

念稿感是「AI 味」的一半：合成前先用廉价模型改写成口语（见 spoken_script），
失败退回原文。
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path

import httpx

from app_core import config

# ---- 两家 provider -------------------------------------------------------
ZHIPU_API = "https://open.bigmodel.cn/api/paas/v4/audio/speech"
ZHIPU_MODEL = "cogtts"
ZHIPU_VOICES = ("tongtong", "female", "xiaochen", "chuichui", "male",
                "jam", "kazi", "douji", "luodo")
DEEPSEEK_API = "https://api.deepseek.com/v1/tools/tts"
DEEPSEEK_VOICES = ("stella", "tide")
# BreezBlue / Breeze TTS 2：支持音色设计、用自然语言"导演"语气；
# 直接请求 pcm_24000，省掉解码，和智谱/DeepSeek 的 PCM 链路一致。
BREEZE_API = "https://api.breeze.blue/v1/text-to-speech"
BREEZE_MODEL = "breeze-tts-2"
PCM_RATE = 24000          # 三家都是 24k mono 16bit，pilk.encode 不用改
MAX_CHARS = 300           # 一条语音别太长（约 30 秒）

# ---- 口语化改写 ----------------------------------------------------------
REWRITE_API = "https://api.deepseek.com/v1/chat/completions"
REWRITE_MODEL = "deepseek-chat"
REWRITE_SYSTEM = (
    "把这句话改写成适合念出来的口语：保留意思和情绪，不要书面词，"
    "可以加语气词；只输出改写后的一句话，不要解释。")

_TTS_CFG = (config.load().get("tts") or {})
PROVIDER = str(_TTS_CFG.get("provider") or "zhipu").strip().lower()
VOICE = str(_TTS_CFG.get("voice") or "").strip()
SPEED = float(_TTS_CFG.get("speed") or 1.0)
FALLBACK = bool(_TTS_CFG.get("fallback", True))
REWRITE = bool(_TTS_CFG.get("script_rewrite", True))
BREEZE_VOICE = str(_TTS_CFG.get("breeze_voice") or "").strip()
BREEZE_INSTRUCTIONS = str(_TTS_CFG.get("breeze_instructions") or "").strip()
# 情绪 → 语气指令：心情/精力/时段会影响 Breeze 怎么"演"这句话。
MOOD_INSTRUCTIONS = bool(_TTS_CFG.get("mood_instructions", True))
# Breeze 的生成控制：guidance 越高越"照着指令演"（1–10，默认 1）；speed 0.5–2。
BREEZE_GUIDANCE = float(_TTS_CFG.get("breeze_guidance") or 1.0)
BREEZE_SPEED = float(_TTS_CFG.get("breeze_speed") or 1.0)
PROVIDERS = ("breeze", "zhipu", "deepseek")
if PROVIDER not in PROVIDERS:
    PROVIDER = "zhipu"


def _key(name: str) -> str:
    from app_core import paths
    try:
        data = json.loads(paths.KEYS.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return ""
    return str(data.get(name) or "").strip()


def voice_for(provider: str) -> str:
    """各 provider 用各自的音色配置；兜底那家用默认音色。"""
    if provider == "breeze":
        return BREEZE_VOICE
    if provider == "zhipu":
        return VOICE if VOICE in ZHIPU_VOICES else "tongtong"
    return VOICE if VOICE in DEEPSEEK_VOICES else "stella"


async def spoken_script(text: str) -> str:
    """书面回复 → 适合朗读的口语；任何失败都退回原文。"""
    t = (text or "").strip()
    if not t or not REWRITE:
        return t
    key = _key("deepseek")
    if not key:
        return t
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(
                REWRITE_API,
                headers={"Authorization": "Bearer " + key},
                json={"model": REWRITE_MODEL,
                      "thinking": {"type": "disabled"},
                      "messages": [{"role": "system", "content": REWRITE_SYSTEM},
                                   {"role": "user", "content": t}],
                      "temperature": 1.0, "max_tokens": 200})
        r.raise_for_status()
        out = (r.json()["choices"][0]["message"]["content"] or "").strip()
        return out or t
    except Exception:  # noqa: BLE001
        return t


async def _pcm_zhipu(text: str, retries: int = 2,
                      mood: dict | None = None) -> bytes | None:
    key = _key("zhipu")
    if not key:
        return None
    payload = {"model": ZHIPU_MODEL, "input": text, "voice": voice_for("zhipu")}
    if SPEED != 1.0:
        payload["speed"] = max(0.5, min(2.0, SPEED))
    return await _post_pcm(ZHIPU_API, key, payload, retries)


def mood_instructions(mood: dict | None, hour: int | None = None) -> str:
    """把情绪/时段翻译成 Breeze 的语气指令（纯函数）。

    只描述"怎么念"，不描述"说什么"——事实层不受影响。任何异常都返回空串。
    """
    if not MOOD_INSTRUCTIONS:
        return ""
    m = mood or {}
    try:
        v = float(m.get("valence") or 0.0)
        e = float(m.get("energy") or 0.5)
        b = float(m.get("valence_base") or 0.0)
    except Exception:  # noqa: BLE001
        v, e, b = 0.0, 0.5, 0.0
    parts: list[str] = []
    if v <= -0.25:
        parts.append("心情偏低：声音放软放轻，语速慢一点，不要劝人")
    elif v >= 0.25 and e >= 0.4:
        # 精力很低时别同时喊「轻快」——两句指令会互相打架（2026-11-16 实测
        # 当时 valence 0.52 / energy 0.09，组合出「轻快 + 轻慢」自相矛盾）。
        parts.append("心情不错：语气轻快一点，带一点笑意")
    if b <= -0.2:
        parts.append("这一阵基调偏低：整体别太亮")
    elif b >= 0.2:
        parts.append("这一阵基调偏暖：自然放松")
    if e <= 0.3:
        # 只轻不拖：2026-11-16 反馈「前段太慢」——低精力改音量，不改语速。
        parts.append("精力低：声音轻一点，语速别拖，别用力")
    elif e >= 0.75:
        parts.append("精力足：语速正常偏快，吐字清楚")
    if hour is None:
        import time as _time
        hour = _time.localtime().tm_hour
    if hour >= 23 or hour < 6:
        parts.append("深夜：压低声音，像怕吵到人")
    elif 6 <= hour <= 9:
        parts.append("早上：声音清一点，别太亢奋")
    return ("语气：" + "；".join(parts)) if parts else ""


def _breeze_instructions(mood: dict | None = None) -> str:
    dynamic = mood_instructions(mood)
    return " ".join(x for x in (BREEZE_INSTRUCTIONS, dynamic) if x).strip()


async def _breeze_request(text: str, output_format: str, retries: int = 2,
                         mood: dict | None = None) -> bytes | None:
    key = _key("breeze")
    voice = voice_for("breeze")
    if not key or not voice:
        return None
    payload = {"text": text, "model_id": BREEZE_MODEL, "language_code": "zh"}
    instructions = _breeze_instructions(mood)
    if instructions:
        payload["instructions"] = instructions
    settings = {}
    if BREEZE_GUIDANCE > 1.0:
        settings["guidance_scale"] = max(1.0, min(10.0, BREEZE_GUIDANCE))
    if BREEZE_SPEED != 1.0:
        settings["speed"] = max(0.5, min(2.0, BREEZE_SPEED))
    if settings:
        payload["voice_settings"] = settings
    last: bytes | None = None
    for attempt in range(retries):
        try:
            async with httpx.AsyncClient(timeout=60) as c:
                r = await c.post(
                    f"{BREEZE_API}/{voice}",
                    headers={"xi-api-key": key},
                    params={"output_format": output_format},
                    json=payload)
            if r.status_code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            r.raise_for_status()
            last = r.content
            break
        except Exception:  # noqa: BLE001
            if attempt < retries - 1:
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            return None
    return last if last and len(last) >= 1600 else None


async def _pcm_breeze(text: str, retries: int = 2,
                      mood: dict | None = None) -> bytes | None:
    return await _breeze_request(text, "pcm_24000", retries, mood)


async def _pcm_deepseek(text: str, retries: int = 2,
                          mood: dict | None = None) -> bytes | None:
    key = _key("deepseek")
    if not key:
        return None
    payload = {"text": text, "voice_id": voice_for("deepseek"),
               "format": "pcm", "filter_markdown": True}
    return await _post_pcm(DEEPSEEK_API, key, payload, retries)


async def _post_pcm(api: str, key: str, payload: dict, retries: int) -> bytes | None:
    for attempt in range(retries):
        try:
            async with httpx.AsyncClient(timeout=40) as c:
                r = await c.post(api, headers={"Authorization": "Bearer " + key},
                                 json=payload)
            if r.status_code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            r.raise_for_status()
            body = r.content
            return body if len(body) >= 1600 else None
        except Exception:  # noqa: BLE001
            if attempt < retries - 1:
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            return None
    return None


_PCM_FUNCS = {"breeze": _pcm_breeze, "zhipu": _pcm_zhipu,
              "deepseek": _pcm_deepseek}


def _silk_from_pcm(pcm: bytes) -> Path | None:
    try:
        import pilk

        out_fd, out_name = tempfile.mkstemp(prefix="tts-", suffix=".silk")
        raw_fd, raw_name = tempfile.mkstemp(prefix="tts-", suffix=".pcm")
        os.close(out_fd)
        out = Path(out_name)
        raw = Path(raw_name)
        with os.fdopen(raw_fd, "wb") as fh:
            fh.write(pcm)
        # tencent=True：写成腾讯系 silk 头（首字节 0x02），QQ/OneBot 才认。
        pilk.encode(str(raw), str(out), pcm_rate=PCM_RATE, tencent=True)
        raw.unlink(missing_ok=True)
        return out if out.exists() and out.stat().st_size > 0 else None
    except Exception:  # noqa: BLE001
        return None


def _pcm_to_opus(pcm: bytes) -> Path | None:
    """PCM → ogg/opus（48k 立体声容器、单声道源；Feishu 直接可播）。"""
    try:
        fd, pcm_name = tempfile.mkstemp(prefix="tts-", suffix=".pcm")
        os.close(fd)
        src = Path(pcm_name)
        src.write_bytes(pcm)
        fd, out_name = tempfile.mkstemp(prefix="tts-", suffix=".opus")
        os.close(fd)
        out = Path(out_name)
        import subprocess

        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "s16le",
                        "-ar", str(PCM_RATE), "-ac", "1", "-i", str(src),
                        "-c:a", "libopus", "-b:a", "64k", "-ar", "48000",
                        str(out)], check=True, timeout=90)
        src.unlink(missing_ok=True)
        return out if out.exists() and out.stat().st_size > 0 else None
    except Exception:  # noqa: BLE001
        return None


async def synth_opus(text: str, *, retries: int = 2,
                     mood: dict | None = None) -> Path | None:
    """给飞书这类直接吃 opus 的通道：Breeze 直接要 opus，不经过 silk 二次转码。

    其它 provider 退回 PCM → ffmpeg 转 opus。
    """
    t = (text or "").strip()
    if not t:
        return None
    t = await spoken_script(t)
    if len(t) > MAX_CHARS:
        t = t[:MAX_CHARS]
    order = [PROVIDER]
    if FALLBACK:
        order += [p for p in PROVIDERS if p != PROVIDER]
    for prov in order:
        if prov == "breeze" and voice_for("breeze"):
            data = await _breeze_request(t, "opus", retries, mood)
            if data:
                fd, name = tempfile.mkstemp(prefix="tts-", suffix=".opus")
                os.close(fd)
                out = Path(name)
                out.write_bytes(data)
                return out
            continue
        pcm = await _PCM_FUNCS[prov](t, retries, mood)
        if pcm:
            return _pcm_to_opus(pcm)
    return None


async def synth(text: str, *, voice: str | None = None,
                retries: int = 2, mood: dict | None = None) -> Path | None:
    """合成一条 silk，返回路径；失败返回 None。

    voice 参数保留兼容旧调用；实际音色由 config.tts.voice 决定。
    """
    t = (text or "").strip()
    if not t:
        return None
    t = await spoken_script(t)
    if len(t) > MAX_CHARS:
        t = t[:MAX_CHARS]

    order = [PROVIDER]
    if FALLBACK:
        order += [p for p in PROVIDERS if p != PROVIDER]
    pcm: bytes | None = None
    for prov in order:
        pcm = await _PCM_FUNCS[prov](t, retries, mood)
        if pcm:
            break
    if not pcm:
        return None
    return _silk_from_pcm(pcm)
