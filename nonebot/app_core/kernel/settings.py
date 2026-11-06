"""配置的只读视图。

P1 只提供一个稳定的读取入口；P6 再换成完整 typed dataclass。
现有的 config.load() 继续保留，避免一次性改所有调用点。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Settings:
    raw: dict

    @classmethod
    def from_mapping(cls, data: dict) -> "Settings":
        return cls(raw=dict(data or {}))

    def get(self, path: str, default: Any = None) -> Any:
        cur: Any = self.raw
        for part in str(path or "").split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def section(self, name: str) -> dict:
        value = self.raw.get(name)
        return value if isinstance(value, dict) else {}
