"""[domain] 关系小抄：称呼、梗、雷区。

几百字，每轮注入，**不走向量检索**——「你又来了」「张老师」这种句子没有主题词，
主题向量永远召不回来（2026-11-12 外部评审的核心判断）。

写入方式是手动编辑 `data/relation.md`：这是关系，不是数据，不该由模型自动改写。
"""
from __future__ import annotations

from pathlib import Path

from app_core import paths

MAX_CHARS = 400


def path() -> Path:
    return paths.DATA / "relation.md"


def load() -> str:
    """读小抄；没有文件或读失败时返回空串（不注入，不报错）。"""
    p = path()
    try:
        if not p.exists():
            return ""
        return p.read_text(encoding="utf-8").strip()[:MAX_CHARS]
    except Exception:  # noqa: BLE001
        return ""
