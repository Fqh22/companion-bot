"""[domain] 语音转写：智谱 GLM-ASR-2512（docs/13）。

只做三件事：转成 16k 单声道 wav、检查时长、调用接口拿文本。
场景判断（人声/音乐/静音）在下一步；这里失败一律返回空文本 + 原因，
不抛异常——一条语音转不出来，不该让整轮对话崩。
"""
from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

import httpx

ASR_API = "https://open.bigmodel.cn/api/paas/v4/audio/transcriptions"
ASR_MODEL = "glm-asr-2512"
MAX_SECONDS = 30.0          # 智谱限制：≤30 秒、≤25MB
MAX_BYTES = 25 * 1024 * 1024


def _keys_file() -> Path:
    from app_core import paths

    return paths.KEYS


def _key() -> str:
    try:
        return str(json.loads(_keys_file().read_text(encoding="utf-8")).get("zhipu") or "")
    except Exception:  # noqa: BLE001
        return ""


def duration(path: Path) -> float:
    """秒；拿不到返回 0（后续按未知处理）。"""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, timeout=20)
        return float((r.stdout or "0").strip() or 0)
    except Exception:  # noqa: BLE001
        return 0.0


def to_wav(src: Path) -> Path | None:
    """任意音频 → 16k 单声道 wav；失败返回 None（格式不支持/文件损坏）。"""
    dst = Path(tempfile.mkstemp(prefix="asr-", suffix=".wav")[1])
    try:
        r = subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-i", str(src),
             "-ar", "16000", "-ac", "1", str(dst)],
            capture_output=True, timeout=120)
        if r.returncode == 0 and dst.exists() and dst.stat().st_size > 1024:
            return dst
    except Exception:  # noqa: BLE001
        pass
    try:
        dst.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass
    return None


async def transcribe(wav: Path) -> dict:
    """返回 {text, error}。text 为空时 error 说明原因，调用方据此兜底。"""
    key = _key()
    if not key:
        return {"text": "", "error": "没有智谱 key"}
    sec = duration(wav)
    if sec and sec > MAX_SECONDS + 0.5:
        return {"text": "", "error": f"太长（{sec:.0f}s > {MAX_SECONDS:.0f}s）"}
    try:
        if wav.stat().st_size > MAX_BYTES:
            return {"text": "", "error": "文件太大"}
        with wav.open("rb") as f:
            async with httpx.AsyncClient(timeout=30) as c:
                r = await c.post(
                    ASR_API,
                    headers={"Authorization": "Bearer " + key},
                    data={"model": ASR_MODEL},
                    files={"file": (wav.name, f, "audio/wav")},
                )
        r.raise_for_status()
        data = r.json()
        return {"text": str(data.get("text") or "").strip(), "error": ""}
    except Exception as e:  # noqa: BLE001
        return {"text": "", "error": f"{type(e).__name__}: {e}"}
