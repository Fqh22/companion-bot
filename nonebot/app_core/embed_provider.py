"""[infra] embedding provider：把「哪家、什么维度、怎么重试」收在一处。

为什么要有这一层：原来 api/model 是 memory.py 的常量 + config 覆盖，
重试逻辑写在 memory_embed.py 里。换一家要改代码、维度不同还不会报错，
只能在召回时静默退化成 recent()（memory.py:177 的兜底）。

维度必须显式声明：换家时旧向量不在同一空间，启动就该报，而不是查了半天「为什么记不住」。
"""
from __future__ import annotations

import asyncio

import httpx

PROVIDERS = {
    "zhipu": {"api": "https://open.bigmodel.cn/api/paas/v4/embeddings",
              "model": "embedding-3", "dim": 2048},
    "siliconflow": {"api": "https://api.siliconflow.cn/v1/embeddings",
                    "model": "BAAI/bge-m3", "dim": 1024},
}


class EmbedError(RuntimeError):
    """配置或协议层面的错，不重试。"""


def spec(provider: str = "", api: str = "", model: str = "", dim: int = 0) -> dict:
    """返回 {api, model, dim}；显式值优先于 provider 默认。"""
    base = PROVIDERS.get(provider) or {}
    return {"api": api or base.get("api", ""),
            "model": model or base.get("model", ""),
            "dim": int(dim or base.get("dim", 0))}


async def embed(texts: list[str], *, api: str, model: str, key: str,
                dim: int = 0, retries: int = 3) -> list[list[float]]:
    """返回与 texts 同序的向量。429/5xx 退避重试，维度不符直接抛。"""
    if not texts:
        return []
    last: Exception | None = None
    for attempt in range(retries):
        try:
            async with httpx.AsyncClient(timeout=60) as c:
                r = await c.post(api, headers={"Authorization": "Bearer " + key},
                                 json={"model": model, "input": texts})
            if r.status_code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            r.raise_for_status()
            data = r.json().get("data", [])
            out = [item.get("embedding") or []
                   for item in sorted(data, key=lambda x: x.get("index", 0))]
            if dim and out and len(out[0]) != dim:
                raise EmbedError("维度不符：配置 %d，实际 %d（换家要重建向量）"
                                 % (dim, len(out[0])))
            return out
        except EmbedError:
            raise
        except Exception as e:  # noqa: BLE001
            last = e
            if attempt < retries - 1:
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            raise
    raise last if last else EmbedError("embed 失败")
