"""EngineConfig：在 import app_core 之前设置运行根目录。

app_core.paths 是 import-time 常量，所以配置必须在创建 Engine 前 apply。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class EngineConfig:
    root: Path

    def apply(self) -> "EngineConfig":
        os.environ["QQBOT_ROOT"] = str(Path(self.root).resolve())
        return self
