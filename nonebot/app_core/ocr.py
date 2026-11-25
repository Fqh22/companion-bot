"""[infra] OCR：DeepSeek 全家桶的 /v1/tools/ocr（2026-11-13 上线）。

为什么单独做：图注现在靠视觉模型「读图里的字」，它是顺带读的，
截图上 30 行报错只能读到 2-3 行。OCR 专精这件事：按页返回全部文字。

不抛异常——OCR 失败就退回「视觉模型自己读」，不该让图注整条失败。
"""
from __future__ import annotations

import json
import mimetypes
import re
from pathlib import Path

from app_core.provider_routes import service_route
import httpx

MAX_BYTES = 20 * 1024 * 1024


def _collect(node, out: list[str], depth: int = 0) -> None:
    """递归收集返回体里的文本字段。

    schema 只说到「每页的识别结果」，具体字段名（text / content / lines）
    没有稳定承诺，所以按结构找而不是写死键名。
    """
    if depth > 6:
        return
    if isinstance(node, str):
        if len(node.strip()) >= 2:
            out.append(node.strip())
    elif isinstance(node, dict):
        for k in ("text", "content", "lines", "blocks", "result", "results", "pages"):
            if k in node:
                _collect(node[k], out, depth + 1)
    elif isinstance(node, list):
        for x in node:
            _collect(x, out, depth + 1)


# 布局标记：rich_layout 模式会返回 <｜ref｜>text<｜/ref｜><｜box｜>[[x,y,x,y]]<｜/box｜> 这种
# 带坐标的片段。图注只要文字，坐标是噪声。
_RE_MARKUP = re.compile(r"<｜box｜>.*?<｜/box｜>|<｜/?[^｜]*｜>", re.S)


async def ocr_text(path: Path, purpose: str = "rich_layout_recognition",
                   timeout: float = 60.0) -> str:
    # 实测：text_recognition 对图片返回空（它主要是给 Word/PPT 直接提文本用的），
    # rich_layout_recognition 逐页识别，图片走这条。代价是返回体带布局标记，
    # 由 _RE_MARKUP 清掉。
    """识别一张图的文字；失败返回空串。"""
    route = service_route("ocr")
    key = route.key()
    if not key or not path or not Path(path).exists():
        return ""
    p = Path(path)
    try:
        if p.stat().st_size > MAX_BYTES:
            return ""
        mime = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        with p.open("rb") as f:
            async with httpx.AsyncClient(timeout=timeout) as c:
                r = await c.post(
                    route.api,
                    headers={"Authorization": "Bearer " + key},
                    data={"purpose": purpose},
                    files={"file": (p.name, f, mime)},
                )
        if r.status_code >= 400:
            return ""
        data = r.json()
    except Exception:  # noqa: BLE001
        return ""
    # 已知结构优先：{"pages": [{"text": "..."}]}
    parts: list[str] = []
    for pg in (data.get("pages") or []):
        if isinstance(pg, dict):
            t = _RE_MARKUP.sub("", str(pg.get("text") or "")).strip()
            if t:
                parts.append(t)
    if not parts:
        # 结构变了再回退到递归收集（字段名没有稳定承诺）
        _collect(data, parts)
        parts = [_RE_MARKUP.sub("", t).strip() for t in parts]
    # 去重保序：嵌套结构常把同一段文字重复收集两次
    seen, uniq = set(), []
    for t in parts:
        if t in seen:
            continue
        seen.add(t)
        uniq.append(t)
    return "\n".join(uniq)[:1200]
