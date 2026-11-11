"""[orchestration] 情绪契约：状态是"单例"，不是"条目"。

为什么和 MemoryPort 分开（见 docs/11）：
- MemoryPort 管可枚举、可检索、可删除的条目；
- mood 是当前状态（valence / valence_base / energy / social），每轮必读，连续衰减，
  `recall` 的列表语义对它不成立。

影子只能调 `read`：`observe` 有副作用（会推高/拉低状态），影子调用会污染真实情绪。
"""
from __future__ import annotations

from typing import Protocol

from app_core import mood


class AffectPort(Protocol):
    def read(self) -> dict: ...

    def observe(self, state: dict, text: str, vec: list | None = None) -> dict: ...

    def write(self, state: dict) -> None: ...


class CoreAffectPort:
    """把 mood.py 适配成状态契约；行为与旧路径完全一致（纯转发）。"""

    def read(self) -> dict:
        return mood.settle(mood.load())

    def observe(self, state: dict, text: str, vec: list | None = None) -> dict:
        return mood.observe(state, text, vec=vec)

    def write(self, state: dict) -> None:
        mood.save(state)
