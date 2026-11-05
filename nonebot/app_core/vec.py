"""向量小工具：打包 / 解包 / 余弦。纯函数，不碰数据库。

从 memory.py 拆出来（2026-11-06）。原因是反向依赖：meme.py（表情包）和
kb.py（知识库）本来就要算余弦、存向量，却得 `from app_core.memory import _pack`
——表情包和知识库不该依赖记忆模块的私有工具。

打包格式是 `float32` 小端裸数组：和 memory.vectors 表的 b"` 列一致，
别在别处另定义一套（改了一处就会读出一堆垃圾浮点数）。
"""
from __future__ import annotations

import math
import struct

FLOAT_BYTES = 4


def pack(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def unpack(blob: bytes) -> list[float]:
    n = len(blob) // FLOAT_BYTES
    return list(struct.unpack(f"{n}f", blob))


def cos(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0
