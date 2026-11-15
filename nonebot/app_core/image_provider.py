"""[infra] 画图：智谱 CogView。

DeepSeek 没有图像生成接口（2026-11-15 核对 70 页文档，vision 是看图不是画图），
所以走 provider 抽象，先接智谱。换家只改这个文件。

失败返回 None——画不出来就说画不出来，不要让整轮回复挂掉。
"""
from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path

import httpx

IMAGE_API = "https://open.bigmodel.cn/api/paas/v4/images/generations"
MODEL = "cogview-3-flash"      # 免费档，带水印；换 cogview-4 要去水印但要计费
SIZE = "1024x1024"
_model = MODEL                  # 测试可替换


def _key() -> str:
    from app_core import paths

    try:
        return str(json.loads(paths.KEYS.read_text(encoding="utf-8")).get("zhipu") or "")
    except Exception:  # noqa: BLE001
        return ""


async def generate(prompt: str, size: str = SIZE, timeout: float = 90.0) -> Path | None:
    """按一句描述画一张图，返回本地临时文件路径；失败返回 None。"""
    p = (prompt or "").strip()[:800]
    key = _key()
    if not p or not key:
        return None
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.post(
                IMAGE_API,
                headers={"Authorization": "Bearer " + key},
                json={"model": _model, "prompt": p, "size": size},
            )
            if r.status_code >= 400:
                return None
            data = r.json()
            url = ((data.get("data") or [{}])[0] or {}).get("url") or ""
            if not url:
                return None
            img = await c.get(url, timeout=timeout)
            if img.status_code >= 400 or not img.content:
                return None
    except Exception:  # noqa: BLE001
        return None
    out = Path(tempfile.mkstemp(prefix="draw-", suffix=".png")[1])
    try:
        out.write_bytes(img.content)
    except Exception:  # noqa: BLE001
        return None
    return out


def _cleanup(path: Path, ttl: float = 3600.0) -> None:
    """删掉一小时前的图：临时目录不该无限攒。"""
    try:
        now = time.time()
        for q in Path(path).parent.glob("draw-*.png"):
            if now - q.stat().st_mtime > ttl:
                q.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass
