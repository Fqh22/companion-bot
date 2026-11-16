"""[infra] 密钥读取：/opt/qqbot-data/keys.json 的唯一入口，按 mtime 缓存。

为什么集中：2026-11 之前有 10 个模块各自 json.loads(paths.KEYS.read_text())，
读盘时机、缓存语义、缺键降级各不相同——llm 有 mtime 缓存，其余没有。
结果是轮换密钥后只有 llm 看到新值，别的模块要等重启才生效；
而每个地方都各写一遍 try/except return ""，缺键时也看不出是哪一步坏的。

契约：
- get(name)：字符串值，缺键/坏文件返回空串（调用方按「没配」降级，不抛）。
- section(name)：子字典（如 feishu 的 app_id/app_secret），缺了返回 {}。
- require(name)：缺键抛 RuntimeError，给「必须有」的路径用（启动自检）。
- 缓存按文件 mtime 失效；文件被删/读失败时返回空而不是旧值。
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

from app_core import paths

log = logging.getLogger("qqbot.keys")

_LOCK = threading.Lock()
_CACHE: dict[str, object] = {"mtime": None, "data": {}}


def path() -> Path:
    """keys.json 的位置（测试要替换时在这里改）。"""
    return paths.KEYS


def _load() -> dict:
    p = paths.KEYS
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return {}
    with _LOCK:
        if _CACHE["mtime"] == mtime:
            return _CACHE["data"]  # type: ignore[return-value]
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {}
        except Exception as e:  # noqa: BLE001
            log.error("读取 keys.json 失败（%s）：%s", p, e)
            data = {}
        _CACHE["mtime"] = mtime
        _CACHE["data"] = data
        return data


def data() -> dict:
    return dict(_load())


def get(name: str, default: str = "") -> str:
    return str(_load().get(name) or default)


def section(name: str) -> dict:
    v = _load().get(name)
    return dict(v) if isinstance(v, dict) else {}


def require(name: str) -> str:
    v = get(name)
    if not v:
        raise RuntimeError(f"缺少 {name} key（{paths.KEYS}）")
    return v
