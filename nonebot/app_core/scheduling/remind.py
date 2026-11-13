"""提醒：一次性定时消息。

时间由模型填成 `YYYY-MM-DD HH:MM`（模型理解"明天九点"比我们写解析器可靠得多）。
这里只负责：存下来、到点取出来、标记已发。
"""
from __future__ import annotations

import re
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

from app_core import paths as _paths

DB = _paths.DATA / "chat.db"

MAX_DAYS = 180          # 最远只接受半年内的提醒
MAX_TEXT = 200


def _db():
    from app_core import remind
    return remind.DB


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(_db(), timeout=15)
    c.execute("PRAGMA journal_mode=WAL")
    return c


def init() -> None:
    c = _conn()
    with c:
        c.execute(
            "CREATE TABLE IF NOT EXISTS reminders ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "text TEXT NOT NULL,"
            "due_ts REAL NOT NULL,"
            "created_ts REAL NOT NULL,"
            "sent INTEGER NOT NULL DEFAULT 0)")
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders(sent, due_ts)")
    c.close()


_CN_NUM = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10, "十一": 11, "十二": 12}


def _num(s: str) -> int | None:
    s = (s or "").strip()
    if s.isdigit():
        return int(s)
    return _CN_NUM.get(s)


def _parse_loose(when: str, now: float | None = None) -> float | None:
    """模型没按 `YYYY-MM-DD HH:MM` 写时的兜底。

    只认几种明确说法（明天 9:00 / 明早九点 / 今晚八点 / 一小时后 / 30 分钟后），
    认不出就返回 None，让 add 继续回「时间看不懂」——**不猜日期**，猜错比报错糟。
    """
    s = (when or "").strip()
    if not s:
        return None
    now = now if now is not None else time.time()
    base = datetime.fromtimestamp(now)

    m = re.search(r"(\d+|[一二两三四五六七八九十]+)\s*(分钟|分|小时|钟头|天)\s*(?:以)?后", s)
    if m:
        n = _num(m.group(1))
        if n is None:
            return None
        unit = m.group(2)
        secs = n * (60 if unit in ("分钟", "分") else 3600 if unit in ("小时", "钟头") else 86400)
        return (base + timedelta(seconds=secs)).timestamp()
    if "半小时后" in s:
        return (base + timedelta(minutes=30)).timestamp()

    day = None
    for word, off in (("大后天", 3), ("后天", 2), ("明天", 1), ("明早", 1),
                      ("明晚", 1), ("今天", 0), ("今晚", 0)):
        if word in s:
            day = off
            break

    period = None
    for word, h in (("凌晨", 0), ("早上", 0), ("上午", 0), ("中午", 12),
                    ("下午", 12), ("傍晚", 12), ("晚上", 12), ("今晚", 12), ("明晚", 12)):
        if word in s:
            period = h
            break

    tm = re.search(r"(\d{1,2}|[一二两三四五六七八九十]{1,2})\s*(?:[:：]|点)\s*"
                   r"(\d{1,2}|[一二三四五六七八九十]{1,2})?\s*分?", s)
    if not tm:
        return None
    hour = _num(tm.group(1))
    minute = _num(tm.group(2)) if tm.group(2) else 0
    if hour is None or minute is None:
        return None
    if period == 12 and hour < 12:
        hour += 12
    if hour > 23 or minute > 59:
        return None

    target = base.replace(hour=0, minute=0, second=0, microsecond=0)
    if day is not None:
        target += timedelta(days=day)
    target = target.replace(hour=hour, minute=minute)
    if day is None and target <= base:
        target += timedelta(days=1)
    return target.timestamp()


def _parse(when: str, now: float | None = None) -> float | None:
    """支持 `YYYY-MM-DD HH:MM`、`YYYY-MM-DD HH:MM:SS`、`YYYY-MM-DD`，再兜底自然语言。"""
    s = (when or "").strip().replace("/", "-").replace("T", " ")
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).timestamp()
        except ValueError:
            continue
    return _parse_loose(s, now)


def _fmt(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")


def _norm(s: str) -> str:
    from app_core.textsim import norm

    return norm(s)


def _similar(a: str, b: str) -> float:
    """两句话有多像。实现在 textsim，提醒和跟进共用一套判据。"""
    from app_core.textsim import similar

    return similar(a, b)


def add(text: str, when: str) -> str:
    text = (text or "").strip()[:MAX_TEXT]
    if not text:
        return "提醒内容不能为空"
    ts = _parse(when)
    if ts is None:
        return f"时间看不懂：{when}（要 YYYY-MM-DD HH:MM）"
    now = time.time()
    if ts < now - 60:
        return f"这个时间已经过去了：{when}"
    if ts > now + MAX_DAYS * 86400:
        return f"太远了，最多提前 {MAX_DAYS} 天"
    init()
    c = _conn()
    # 近重复拦截：同一件事换个说法又说一遍，不该变成两条提醒。
    # 实测踩过：两条措辞略不同的「改那个 bug」都建了，到点各响一次。
    try:
        rows = c.execute(
            "SELECT id, text FROM reminders WHERE sent=0 AND ABS(due_ts-?) < 3600",
            (ts,)).fetchall()
        for rid0, t0 in rows:
            if _similar(t0, text) >= 0.6:
                c.close()
                return f"已经有一条差不多的提醒了（#{rid0}）：{t0}"
    except Exception:  # noqa: BLE001
        pass
    with c:
        cur = c.execute("INSERT INTO reminders(text, due_ts, created_ts) VALUES (?,?,?)",
                        (text, ts, now))
        rid = cur.lastrowid
    c.close()
    # 联动：提醒也进 followup，这样到点提醒过之后，主动消息还能接着问进展
    try:
        from app_core import followup
        followup.add(text, "reminder", str(rid))
    except Exception:
        pass
    return f"记下了：{_fmt(ts)} 提醒你「{text}」"


def due_now(now: float | None = None) -> list[dict]:
    """取已到期且未发送的。"""
    now = now or time.time()
    init()
    c = _conn()
    rows = c.execute(
        "SELECT id, text, due_ts FROM reminders WHERE sent=0 AND due_ts <= ? "
        "ORDER BY due_ts LIMIT 10", (now,)).fetchall()
    c.close()
    return [{"id": r[0], "text": r[1], "due_ts": r[2]} for r in rows]


def claim(rid: int) -> bool:
    """先抢占再发送：UPDATE 带 sent=0 条件，只有真正改到行的那一方才发。

    这样即使重启后同一分钟重跑、或者以后跑多实例，也不会重复发同一条提醒。
    """
    c = _conn()
    with c:
        cur = c.execute("UPDATE reminders SET sent=1 WHERE id=? AND sent=0", (rid,))
        n = cur.rowcount
    c.close()
    return n > 0


def release(rid: int) -> None:
    """发送失败就把抢占还回去，下一分钟继续试。"""
    c = _conn()
    with c:
        c.execute("UPDATE reminders SET sent=0 WHERE id=?", (rid,))
    c.close()


def mark_sent(rid: int) -> None:
    c = _conn()
    with c:
        c.execute("UPDATE reminders SET sent=1 WHERE id=?", (rid,))
    c.close()


def list_pending(limit: int = 10) -> list[dict]:
    """还没到点的，按时间正序。"""
    init()
    c = _conn()
    rows = c.execute(
        "SELECT id, text, due_ts FROM reminders WHERE sent=0 AND due_ts > ? "
        "ORDER BY due_ts LIMIT ?", (time.time(), limit)).fetchall()
    c.close()
    return [{"id": r[0], "text": r[1], "due_ts": r[2]} for r in rows]


def cancel(rid: int) -> str:
    """取消一条还没发的提醒（直接删）。已发/不存在的返回提示。

    联动：提醒在 add 时会被复制成一条 followup（用于「到点之后还能问进展」）。
    这里要把它一起取消——否则提醒取消了、跟进还开着，主动消息过几天会去问
    「你那个提醒的事怎么样了」，而这件事根本没发生过。
    """
    c = _conn()
    with c:
        cur = c.execute("DELETE FROM reminders WHERE id=? AND sent=0", (rid,))
        n = cur.rowcount
    c.close()
    if not n:
        return f"没有这条提醒：{rid}（可能已经发过了）"
    try:
        from app_core import followup

        followup.cancel_ref(str(rid))
    except Exception:
        pass
    return "已取消"


def describe_pending(limit: int = 10) -> str:
    items = list_pending(limit)
    if not items:
        return "现在没有待提醒的事"
    return "\n".join(f"[{i['id']}] {_fmt(i['due_ts'])} {i['text']}" for i in items)
