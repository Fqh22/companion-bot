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
import tempfile
from pathlib import Path

import httpx

TTS_API = "https://open.bigmodel.cn/api/paas/v4/audio/speech"
TTS_MODEL = "cogtts"
PCM_RATE = 24000          # 智谱 TTS 默认 24k mono 16bit
MAX_CHARS = 300           # 一条语音别太长（约 30 秒）


def _key() -> str:
    from app_core import paths
    try:
        return str(json.loads(paths.KEYS.read_text(encoding="utf-8")).get("zhipu") or "")
    except Exception:  # noqa: BLE001
        return ""


async def synth(text: str, *, voice: str = "female",
                model: str = TTS_MODEL, retries: int = 2) -> Path | None:
    """合成一条 silk，返回路径；失败返回 None。"""
    t = (text or "").strip()
    if not t:
        return None
    key = _key()
    if not key:
        return None
    if len(t) > MAX_CHARS:
        t = t[:MAX_CHARS]
    pcm: bytes | None = None
    for attempt in range(retries):
        try:
            async with httpx.AsyncClient(timeout=40) as c:
                r = await c.post(TTS_API, headers={"Authorization": "Bearer " + key},
                                 json={"model": model, "input": t, "voice": voice})
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

        out = Path(tempfile.mkstemp(prefix="tts-", suffix=".silk")[1])
        raw = Path(tempfile.mkstemp(prefix="tts-", suffix=".pcm")[1])
        raw.write_bytes(pcm)
        pilk.encode(str(raw), str(out), pcm_rate=PCM_RATE)
        raw.unlink(missing_ok=True)
        return out if out.exists() and out.stat().st_size > 0 else None
    except Exception:  # noqa: BLE001
        return None
