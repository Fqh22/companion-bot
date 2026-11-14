"""embedding 适配：HTTP 调用 + 进程内缓存。"""
from __future__ import annotations

import hashlib
import logging

import asyncio

import httpx

from app_core.memory_common import EMBED_API, EMBED_MODEL

log = logging.getLogger("qqbot.memory")
_EMBED_CACHE: dict[str, list[float]] = {}
EMBED_CACHE_MAX = 256


class EmbedMixin:
    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        out: list[list[float] | None] = [None] * len(texts)
        todo: list[tuple[int, str, str]] = []
        for i, t in enumerate(texts):
            if not (t or "").strip():
                # 空串会让智谱直接 400（code 1210），一个空块就能毒掉整批请求
                out[i] = []
                continue
            key = hashlib.sha1((t or "").encode("utf-8")).hexdigest()
            hit = _EMBED_CACHE.get(key)
            if hit is not None:
                out[i] = hit
            else:
                todo.append((i, key, t))
        if todo:
            # 429 是限流不是故障：实测智谱在高峰时段连续返回 429，
            # 一次失败就让「写记忆失败，水位不动」——退避重试能救回大部分。
            data = []
            last: Exception | None = None
            for attempt in range(3):
                try:
                    async with httpx.AsyncClient(timeout=60) as client:
                        r = await client.post(
                            self.api,
                            headers={"Authorization": f"Bearer {self.key}"},
                            json={"model": self.model, "input": [t for _, _, t in todo]},
                        )
                    if r.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                        await asyncio.sleep(1.5 * (attempt + 1))
                        continue
                    r.raise_for_status()
                    data = r.json().get("data", [])
                    break
                except Exception as e:  # noqa: BLE001
                    last = e
                    if attempt < 2:
                        await asyncio.sleep(1.5 * (attempt + 1))
                        continue
                    raise
            else:
                if last:
                    raise last
            for item in data:
                pos = item.get("index", 0)
                if 0 <= pos < len(todo):
                    i, key, _ = todo[pos]
                    out[i] = item.get("embedding")
                    if len(_EMBED_CACHE) >= EMBED_CACHE_MAX:
                        _EMBED_CACHE.clear()
                    _EMBED_CACHE[key] = out[i]
        return [v or [] for v in out]
