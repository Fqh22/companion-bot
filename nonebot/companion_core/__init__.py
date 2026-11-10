"""companion-core：稳定入口，内部仍复用 app_core。

M6-mini 先做门面与可安装包；后续再把 app_core 物理搬进来。
"""
from __future__ import annotations

from companion_core.config import EngineConfig
from companion_core.factory import create_engine

__version__ = "0.1.0"
__all__ = ["EngineConfig", "create_engine", "__version__"]
