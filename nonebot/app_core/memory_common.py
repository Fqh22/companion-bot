"""memory 共享常量/日志：避免各拆分包循环 import。"""
from __future__ import annotations

import json
import logging

from app_core import paths as _paths

log = logging.getLogger("qqbot.memory")
EMBED_API = "https://open.bigmodel.cn/api/paas/v4/embeddings"
EMBED_MODEL = "embedding-3"
PERSONA_ID = "editor-assistant"
SHADOW_DEDUP_COS = 0.85


def _zhipu_key() -> str:
    try:
        return json.loads(_paths.KEYS.read_text(encoding="utf-8")).get("zhipu", "")
    except Exception:
        return ""
