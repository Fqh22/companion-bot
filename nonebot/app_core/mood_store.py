"""mood 的状态文件与历史表读写。

路径通过 app_core.mood 动态读取，测试对 mood.STATE / CHAT_DB 的补丁仍然有效。
"""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timedelta

from app_core.mood_domain import (  # noqa: F401
    DEFAULT, ENERGY_REST, PROGRESS_DELTA, PROGRESS_ENERGY,
    _bump, _note, describe, settle, task_cost,
)

HISTORY_KEEP_DAYS = 400


def _state():
    from app_core import mood
    return mood.STATE


def _state_dir():
    from app_core import mood
    return mood.STATE_DIR


def _chat_db():
    from app_core import mood
    return mood.CHAT_DB


def load() -> dict:
    data: dict = {}
    try:
        if _state().exists():
            data = json.loads(_state().read_text(encoding="utf-8")) or {}
    except Exception:
        data = {}
    s = {**DEFAULT, **data}
    # 老状态文件没有基线：拿当前心情当基线，避免第一次 settle 就把它拽回 0.1
    if "valence_base" not in data:
        s["valence_base"] = float(s.get("valence") or 0.0)
    s["why"] = list(s.get("why") or [])[-3:]
    return s

def save(s: dict) -> None:
    try:
        _state_dir().mkdir(parents=True, exist_ok=True)
        _state().write_text(json.dumps(s, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass

def apply_task(rounds: int, chars: int) -> None:
    """回复生成完之后结算任务成本。"""
    cost = task_cost(rounds, chars)
    if cost <= 0:
        return
    try:
        s = settle(load())
        _bump(s, "energy", -cost)
        if cost >= 0.015:
            _note(s, "刚才那轮查得多、答得长", -1)
        save(s)
    except Exception:
        pass

def apply_progress(kind: str) -> None:
    """总结模型对「这段对话解决了没有」的判断，折算成情绪变化。

    和逐条余弦互补：余弦看相邻两句，这个看整段 8 轮。
    """
    d = PROGRESS_DELTA.get(kind or "")
    if d is None:
        return
    try:
        s = settle(load())
        _bump(s, "valence", d)
        e = PROGRESS_ENERGY.get(kind)
        if e:
            _bump(s, "energy", e)
        _note(s, {"advance": "这段聊得顺", "repeat": "这段在一个问题上打转",
                  "switch": "这段换了话题"}[kind], 1 if d > 0 else -1)
        save(s)
    except Exception:
        pass

def _db() -> sqlite3.Connection:
    c = sqlite3.connect(_chat_db(), timeout=15)
    c.execute("PRAGMA journal_mode=WAL")
    return c

def _init_history(c: sqlite3.Connection) -> None:
    with c:
        c.execute(
            "CREATE TABLE IF NOT EXISTS mood_history ("
            "day TEXT PRIMARY KEY, valence REAL NOT NULL, valence_base REAL NOT NULL,"
            "energy REAL NOT NULL, social REAL NOT NULL, msgs INTEGER DEFAULT 0,"
            "events INTEGER DEFAULT 0, ts REAL NOT NULL)")

def snapshot(s: dict | None = None, day: str = "") -> dict:
    """把当天的心情存一行。同一天重复调用是覆盖（晚上比白天更准），不会堆行。"""
    now = time.time()
    if s is None:
        s = settle(load(), now)
    day = day or datetime.now().strftime("%Y-%m-%d")
    row = {
        "day": day,
        "valence": round(float(s.get("valence", 0.0)), 4),
        "valence_base": round(float(s.get("valence_base", 0.0)), 4),
        "energy": round(float(s.get("energy", ENERGY_REST)), 4),
        "social": round(float(s.get("social", 0.5)), 4),
        "msgs": int(s.get("msgs_today") or 0),
        "events": int(s.get("events_today") or 0),
    }
    c = _db()
    _init_history(c)
    with c:
        c.execute(
            "INSERT OR REPLACE INTO mood_history"
            "(day, valence, valence_base, energy, social, msgs, events, ts) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (day, row["valence"], row["valence_base"], row["energy"], row["social"],
             row["msgs"], row["events"], now))
        # 只留一年出头：再久远的快照对调参没帮助，反而拖慢备份
        cutoff = (datetime.now() - timedelta(days=HISTORY_KEEP_DAYS)).strftime("%Y-%m-%d")
        c.execute("DELETE FROM mood_history WHERE day < ?", (cutoff,))
    c.close()
    return row

def recent(days: int = 7) -> list[dict]:
    """最近 N 天的快照，按时间正序返回（画表、算均值都方便）。"""
    c = _db()
    _init_history(c)
    rows = c.execute(
        "SELECT day, valence, valence_base, energy, social, msgs, events "
        "FROM mood_history ORDER BY day DESC LIMIT ?", (max(1, int(days)),)).fetchall()
    c.close()
    out = [{"day": r[0], "valence": r[1], "valence_base": r[2], "energy": r[3],
            "social": r[4], "msgs": r[5], "events": r[6]} for r in rows]
    return list(reversed(out))

def summary(days: int = 7) -> dict:
    """把一段时间的情绪压成几个数：均值、最好/最差的一天。"""
    rows = recent(days)
    if not rows:
        return {"days": 0}
    best = max(rows, key=lambda r: r["valence"])
    worst = min(rows, key=lambda r: r["valence"])
    return {
        "days": len(rows),
        "valence": round(sum(r["valence"] for r in rows) / len(rows), 3),
        "base": round(sum(r["valence_base"] for r in rows) / len(rows), 3),
        "energy": round(sum(r["energy"] for r in rows) / len(rows), 3),
        "social": round(sum(r["social"] for r in rows) / len(rows), 3),
        "msgs": sum(int(r["msgs"] or 0) for r in rows),
        "best": best,
        "worst": worst,
    }
