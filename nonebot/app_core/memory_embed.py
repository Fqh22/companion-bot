"""[infra] embedding 适配：HTTP 调用 + 进程内缓存。"""
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
            # 重试 / 429 / 维度校验都在 embed_provider 里：换一家只改配置，
            # 不用在这里再写一套退避（2026-11-14 一夜之间三处各写了一遍）。
            from app_core import embed_provider

            vecs = await embed_provider.embed(
                [t for _, _, t in todo], api=self.api, model=self.model,
                key=self.key, dim=getattr(self, "dim", 0))
            for (i, key, _), vec in zip(todo, vecs):
                out[i] = vec
                if len(_EMBED_CACHE) >= EMBED_CACHE_MAX:
                    _EMBED_CACHE.clear()
                _EMBED_CACHE[key] = vec
        return [v or [] for v in out]
