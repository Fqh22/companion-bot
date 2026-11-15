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


async def _pcm_zhipu(text: str, retries: int = 2) -> bytes | None:
    key = _key("zhipu")
    if not key:
        return None
    payload = {"model": ZHIPU_MODEL, "input": text, "voice": voice_for("zhipu")}
    if SPEED != 1.0:
        payload["speed"] = max(0.5, min(2.0, SPEED))
    return await _post_pcm(ZHIPU_API, key, payload, retries)


async def _pcm_breeze(text: str, retries: int = 2) -> bytes | None:
    key = _key("breeze")
    voice = voice_for("breeze")
    if not key or not voice:
        return None
    payload = {"text": text, "model_id": BREEZE_MODEL, "language_code": "zh"}
    if BREEZE_INSTRUCTIONS:
        payload["instructions"] = BREEZE_INSTRUCTIONS
    last: bytes | None = None
    for attempt in range(retries):
        try:
            async with httpx.AsyncClient(timeout=60) as c:
                r = await c.post(
                    f"{BREEZE_API}/{voice}",
                    headers={"xi-api-key": key},
                    params={"output_format": "pcm_24000"},
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


async def _pcm_deepseek(text: str, retries: int = 2) -> bytes | None:
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


async def synth(text: str, *, voice: str | None = None,
                retries: int = 2) -> Path | None:
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
        pcm = await _PCM_FUNCS[prov](t, retries)
        if pcm:
            break
    if not pcm:
        return None
    try:
        import pilk

        # mkstemp 的 fd 必须显式接管：丢掉会每合成一条泄漏一个 fd。
        out_fd, out_name = tempfile.mkstemp(prefix="tts-", suffix=".silk")
        raw_fd, raw_name = tempfile.mkstemp(prefix="tts-", suffix=".pcm")
        os.close(out_fd)
        out = Path(out_name)
        raw = Path(raw_name)
        with os.fdopen(raw_fd, "wb") as f:
            f.write(pcm)
        pilk.encode(str(raw), str(out), pcm_rate=PCM_RATE)
        raw.unlink(missing_ok=True)
        return out if out.exists() and out.stat().st_size > 0 else None
    except Exception:  # noqa: BLE001
        return None
