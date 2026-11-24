"""EngineConfig：在 import app_core 之前设置运行根目录。

app_core.paths 是 import-time 常量，所以配置必须在创建 Engine 前 apply。
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class EngineConfig:
    root: Path

    def apply(self) -> "EngineConfig":
        root = Path(self.root).resolve()
        paths = sys.modules.get("app_core.paths")
        if paths is not None and paths.ROOT.resolve() != root:
            raise RuntimeError("运行根目录已初始化，切换数据目录需要启动独立进程")
        os.environ["QQBOT_ROOT"] = str(root)
        return self
