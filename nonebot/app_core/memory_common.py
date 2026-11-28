"""[domain] memory 共享常量/日志：避免各拆分包循环 import。"""
from __future__ import annotations

import logging

from app_core.kernel import keys

log = logging.getLogger("qqbot.memory")
EMBED_API = "https://open.bigmodel.cn/api/paas/v4/embeddings"
EMBED_MODEL = "embedding-3"
PERSONA_ID = "editor-assistant"


def _zhipu_key() -> str:
    return keys.get("zhipu")
