"""[infra] 语音合成：智谱 TTS → PCM → silk（QQ 语音格式）。

链路：text --(智谱 /audio/speech)--> PCM 24k mono
      --(pilk.encode)--> silk（#!SILK_V，QQ 能直接发）

为什么不用 mp3/wav 中转：QQ 的 record 只认 silk（amr 也认，但体积大、音质差），
而 ffmpeg 不支持 silk 编码——只有 pilk 能写。

失败返回 None，不抛：一条语音合成失败不该让整轮回复崩。
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path

import httpx

# 2026-11-15 换到 DeepSeek 全家桶：/v1/tools/tts，返回 PCM 24k（和智谱一样）。
# 换的理由是「去单点」：智谱欠费那晚语音和记忆一起挂了。
TTS_API = "https://api.deepseek.com/v1/tools/tts"
VOICE_ID = "stella"       # stella 女声 / tide 男声
PCM_RATE = 24000          # 两家都是 24k mono 16bit，pilk.encode 不用改
MAX_CHARS = 300           # 一条语音别太长（约 30 秒）

# 念稿感是「AI 味」的一半：书面回复直接朗读会像播报。合成前先用廉价模型
# 改写成口语（保留意思和情绪），失败就退回原文——不能因为改写挂掉语音。
REWRITE_API = "https://api.deepseek.com/v1/chat/completions"
REWRITE_MODEL = "deepseek-chat"
REWRITE_SYSTEM = (
    "把这句话改写成适合念出来的口语：保留意思和情绪，不要书面词，"
    "可以加语气词；只输出改写后的一句话，不要解释。")

from app_core import config  # noqa: E402

_TTS_CFG = (config.load().get("tts") or {})
REWRITE = bool(_TTS_CFG.get("script_rewrite", True))


def _key() -> str:
    from app_core import paths
    try:
        return str(json.loads(paths.KEYS.read_text(encoding="utf-8")).get("deepseek") or "")
    except Exception:  # noqa: BLE001
        return ""


async def spoken_script(text: str) -> str:
    """书面回复 → 适合朗读的口语；任何失败都退回原文。"""
    t = (text or "").strip()
    if not t or not REWRITE:
        return t
    key = _key()
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


async def synth(text: str, *, voice: str = VOICE_ID,
                retries: int = 2) -> Path | None:
    """合成一条 silk，返回路径；失败返回 None。"""
    t = (text or "").strip()
    if not t:
        return None
    key = _key()
    if not key:
        return None
    t = await spoken_script(t)
    if len(t) > MAX_CHARS:
        t = t[:MAX_CHARS]
    pcm: bytes | None = None
    for attempt in range(retries):
        try:
            async with httpx.AsyncClient(timeout=40) as c:
                # format=pcm：直接拿 16-bit mono 24k，省一次 wav 解包
                r = await c.post(TTS_API, headers={"Authorization": "Bearer " + key},
                                 json={"text": t, "voice_id": voice,
                                       "format": "pcm", "filter_markdown": True})
            if r.status_code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            r.raise_for_status()
            pcm = r.content
            break
        except Exception:  # noqa: BLE001
            if attempt < retries - 1:
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            return None
    if not pcm or len(pcm) < 1600:
        return None
    try:
        import pilk

        # mkstemp 的 fd 必须显式接管：丢掉会每合成一条泄漏一个 fd（最终 EMFILE）。
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
