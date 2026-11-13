"""[orchestration] 感知层 v1：外部现实的最小入口。

契约（见 docs/15）：
    read(topic) -> {"text", "as_of", "source"} | None
None 表示 **unknown**，不返回空字符串——缺口要显式，但 prompt 层不常驻。

v1 只接天气：wttr.in 免 key，失败退到 web.search；缓存 30 分钟。
日历/位置/通知没有客户端，不做（不要写永远 unknown 的桩）。
"""
from __future__ import annotations

import logging
import re
import time

from app_core import config

log = logging.getLogger("qqbot.perception")

# 命中这些词才查——不是每轮都碰外部 API。词表故意窄，
# 「冷/热」单字会误伤「冷知识」「热饭」，用词组。
_WORDS = ("天气", "下雨", "降温", "气温", "穿衣", "出门", "刮风",
          "下雪", "晴天", "阴天", "好冷", "好热", "有点冷", "有点热",
          "带伞", "冷不冷", "热不热")

_CACHE: dict[str, tuple[float, dict | None]] = {}


def _cfg() -> dict:
    return dict(config.load().get("weather") or {})


def city() -> str:
    return str(_cfg().get("city") or "").strip()


def _ttl() -> float:
    try:
        return max(5.0, float(_cfg().get("cache_minutes", 30))) * 60
    except Exception:  # noqa: BLE001
        return 1800.0


def should_read(text: str) -> bool:
    return any(w in (text or "") for w in _WORDS)


async def _wttr(city_name: str) -> dict | None:
    try:
        import httpx

        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.get(f"https://wttr.in/{city_name}",
                            params={"format": "%C+%t+%w+%h"})
            r.raise_for_status()
            # wttr 的 format 用 + 分隔：Heavy rain++23°C+←10km/h+96%
            text = re.sub(r"\s*\+\s*", " ", (r.text or "").strip()).strip()
        if not text or "Unknown" in text or len(text) > 120:
            return None
        return {"text": text, "as_of": time.time(), "source": "wttr.in"}
    except Exception as e:  # noqa: BLE001
        log.debug("wttr 查询失败：%s", e)
        return None


async def _search(city_name: str) -> dict | None:
    try:
        from app_core import web

        res = await web.search(f"{city_name}今天天气", 3)
        if not res or res.startswith(("搜索失败", "没搜到")):
            return None
        return {"text": res[:300], "as_of": time.time(), "source": "search"}
    except Exception:  # noqa: BLE001
        return None


async def read(city_name: str | None = None) -> dict | None:
    """查天气；失败返回 None（unknown），由调用方决定怎么说。"""
    name = (city_name or city()).strip()
    if not name:
        return None
    now = time.time()
    hit = _CACHE.get(name)
    if hit and now - hit[0] < _ttl():
        return hit[1]
    data = await _wttr(name) or await _search(name)
    _CACHE[name] = (now, data)
    return data


def clear_cache() -> None:
    _CACHE.clear()
