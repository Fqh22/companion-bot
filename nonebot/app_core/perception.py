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
from app_core import paths as _paths

log = logging.getLogger("qqbot.perception")

# 命中这些词才查——不是每轮都碰外部 API。词表故意窄，
# 「冷/热」单字会误伤「冷知识」「热饭」，用词组。
_WORDS = ("天气", "下雨", "降温", "气温", "穿衣", "出门", "刮风",
          "下雪", "晴天", "阴天", "好冷", "好热", "有点冷", "有点热",
          "带伞", "冷不冷", "热不热")

_CACHE: dict[str, tuple[float, dict | None]] = {}
DB = _paths.CHAT_DB


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

_STATE = _paths.DATA / "weather_state.json"
_ALERT = _paths.DATA / "weather_alert.json"


def _load(path):
    try:
        import json
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _save(path, data) -> None:
    try:
        import json
        _paths.DATA.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def mentioned_today(session: str) -> bool:
    """他今天提过天气/出门/穿衣吗？天气突变只在这种日子开口。"""
    from datetime import datetime
    import sqlite3

    now = datetime.now()
    since = datetime(now.year, now.month, now.day).timestamp()
    sql = "SELECT content FROM messages WHERE role='user' AND ts>=?"
    args: list = [since]
    if session:
        sql += " AND session=?"
        args.append(session)
    try:
        c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=10)
        rows = c.execute(sql, args).fetchall()
        c.close()
    except Exception:  # noqa: BLE001
        return False
    return any(should_read(r[0]) for r in rows)


def alert_allowed(now=None) -> bool:
    """一周最多一次；只在 8-21 点。"""
    from datetime import datetime

    now = now or datetime.now()
    if not (8 <= now.hour < 21):
        return False
    return _load(_ALERT).get("week") != now.strftime("%Y-W%W")


def mark_alert(now=None) -> None:
    from datetime import datetime

    now = now or datetime.now()
    _save(_ALERT, {"week": now.strftime("%Y-W%W"), "ts": time.time()})


def _temp(text: str) -> int | None:
    m = re.search(r"(-?\d+)\s*°C", text or "")
    return int(m.group(1)) if m else None


async def change() -> dict | None:
    """跨天比较温度；降 5 度以上返回突变信息。同一天只更新基线，不重复报。"""
    from datetime import datetime

    data = await read()
    temp = _temp((data or {}).get("text") or "")
    if temp is None:
        return None
    today = datetime.now().strftime("%Y-%m-%d")
    prev = _load(_STATE)
    if prev.get("date") and prev.get("date") != today and prev.get("temp") is not None:
        delta = temp - int(prev["temp"])
        _save(_STATE, {"date": today, "temp": temp, "ts": time.time()})
        if delta <= -5:
            return {"delta": delta, "temp": temp, "prev": prev["temp"],
                    "text": data.get("text") or ""}
        return None
    _save(_STATE, {"date": today, "temp": temp, "ts": time.time()})
    return None
