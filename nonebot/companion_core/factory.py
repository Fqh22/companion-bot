"""工厂：按配置创建 Engine；core 可注入，便于测试/复用。"""
from __future__ import annotations

from companion_core.config import EngineConfig


def create_engine(config: EngineConfig | None = None, core=None):
    if config is not None:
        config.apply()
    from app_core.engine import Engine
    if core is None:
        from app_core.chat import ChatCore
        core = ChatCore()
    return Engine(core)
