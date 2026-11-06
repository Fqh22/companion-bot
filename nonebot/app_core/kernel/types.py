"""跨模块的纯数据类型：不依赖 sqlite / httpx / nonebot。"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class MemoryItem:
    doc_id: str
    text: str
    meta: dict = field(default_factory=dict)
    score: float = 0.0
    age_days: float | None = None


@dataclass
class Message:
    role: str
    content: str


@dataclass
class TurnInput:
    session: str
    text: str
    images: list[str] = field(default_factory=list)
    store_text: str | None = None
