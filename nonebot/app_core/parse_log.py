"""[infra] 模型输出格式漂移的统一记录。

解析失败不抛、不打断回复，但必须留痕：静默失败会让「它记混了」永远查不出来。
只记最近的事实，不参与任何业务逻辑。文件按大小轮转，避免长期跑成几 MB。
"""
from __future__ import annotations

import json
import logging
import time

from app_core import paths as _paths

LOG = _paths.DATA / "parse_fail.jsonl"
MAX_BYTES = 512 * 1024
log = logging.getLogger("qqbot.parse_log")


def note(kind: str, raw: str, reason: str = "") -> None:
    """记一条解析失败：kind=summary/contradict/profile/review/self_check。"""
    try:
        _paths.DATA.mkdir(parents=True, exist_ok=True)
        try:
            if LOG.stat().st_size > MAX_BYTES:
                lines = LOG.read_text(encoding="utf-8", errors="replace").splitlines()
                LOG.write_text(chr(10).join(lines[-400:]) + chr(10), encoding="utf-8")
        except FileNotFoundError:
            pass
        with LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.time(), "kind": kind, "reason": reason,
                                "sample": (raw or "")[:300]}, ensure_ascii=False) + chr(10))
    except Exception as e:  # noqa: BLE001
        log.debug("parse_fail 记录失败：%s", e)


def recent(n: int = 20) -> list[dict]:
    """最近 n 条，给 /debug 或周报用。"""
    try:
        lines = LOG.read_text(encoding="utf-8", errors="replace").splitlines()[-max(1, n):]
    except FileNotFoundError:
        return []
    out = []
    for ln in lines:
        try:
            out.append(json.loads(ln))
        except Exception:  # noqa: BLE001
            continue
    return out