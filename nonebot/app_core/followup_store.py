"""followup 的 SQLite 读写；DB 通过 app_core.followup 动态读取。"""
from __future__ import annotations

import logging
import sqlite3
import time

from app_core import textsim
from app_core.followup_domain import (  # noqa: F401
    ASK_MAX_AGE_DAYS, ASK_MAX_TIMES, CLOSED, COOLDOWN_DAYS, KEEP_MAX_OPEN,
    AFFIRM, MAX_AGE_DAYS, MAX_ASKS, MAX_TEXT, NEAR_DUP, STATUSES,
    _overlap, askable, resolve_from,
)

log = logging.getLogger("qqbot.followup")


def _conn() -> sqlite3.Connection:
    from app_core import followup
    c = sqlite3.connect(followup.DB, timeout=15)
    c.execute("PRAGMA journal_mode=WAL")
    return c

def init() -> None:
    c = _conn()
    with c:
        c.execute(
            "CREATE TABLE IF NOT EXISTS followups ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "text TEXT NOT NULL,"
            "source TEXT NOT NULL DEFAULT 'memory',"   # memory | reminder
            "ref_id TEXT DEFAULT '',"                  # 记忆 doc_id 或提醒 id
            "created_ts REAL NOT NULL,"
            "last_asked_ts REAL NOT NULL DEFAULT 0,"
            "ask_count INTEGER NOT NULL DEFAULT 0,"
            "done INTEGER NOT NULL DEFAULT 0)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_followup_open "
                  "ON followups(done, last_asked_ts)")
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_followup_text "
                  "ON followups(text)")
        # 老库补列：status 是权威状态，done 由它派生
        for ddl in (
            "ALTER TABLE followups ADD COLUMN status TEXT NOT NULL DEFAULT 'todo'",
            "ALTER TABLE followups ADD COLUMN status_ts REAL NOT NULL DEFAULT 0",
        ):
            try:
                c.execute(ddl)
            except Exception:
                pass
        c.execute("UPDATE followups SET status='done' "
                  "WHERE done=1 AND status='todo'")
    c.close()

def add(text: str, source: str = "memory", ref_id: str = "") -> bool:
    """加一条。**文本相同或近似**都跳过。

    只靠唯一索引挡不住「换个说法」：实测同一件事进过两条
    （「改那个 bug 先加日志」/「改那个主动回复不进上下文的 bug，先加日志再复现」）。
    所以在 INSERT 之前先跟未关闭的条目比一次相似度。
    """
    t = (text or "").strip()[:MAX_TEXT]
    if not t:
        return False
    # 写入侧和读取侧用同一道判据：summary 的 followups 是模型自由写的，
    # 光靠 prompt 约束不住（线上 30 条里一半不合格）。
    if not askable(t):
        log.debug("跟进不像他的待办，跳过：%s", t[:40])
        return False
    init()
    c = _conn()
    try:
        try:
            from app_core.textsim import similar

            rows = c.execute(
                "SELECT text FROM followups WHERE done=0 AND status!='cancelled'"
            ).fetchall()
            for (old,) in rows:
                if similar(old, t) >= NEAR_DUP:
                    log.debug("跟进近似重复，跳过：%s ≈ %s", t[:20], old[:20])
                    return False
        except Exception:  # noqa: BLE001
            pass                                  # 相似度算不出来也不影响写入
        cur = c.execute(
            "INSERT OR IGNORE INTO followups(text, source, ref_id, created_ts) "
            "VALUES (?,?,?,?)", (t, source, str(ref_id), time.time()))
        c.commit()
        # 用 rowcount 而不是 total_changes：后者是连接级累计值，判断不准
        return cur.rowcount > 0
    except Exception:
        return False
    finally:
        c.close()

def add_many(items: list[str], source: str = "memory", ref_id: str = "") -> int:
    n = 0
    for it in items or []:
        if add(it, source, ref_id):
            n += 1
    return n

def due_for_asking(limit: int = 5, cooldown_days: float = COOLDOWN_DAYS) -> list[dict]:
    """还没关闭、且距上次追问够久的事项。优先「从没问过」的。"""
    init()
    now = time.time()
    cutoff = now - cooldown_days * 86400
    fresh = now - ASK_MAX_AGE_DAYS * 86400
    c = _conn()
    # 多取一些再按语义过滤：过滤在 Python 侧，SQL 先按时间/次数/冷却筛。
    # 排序改成「没问过的优先，新的优先」——原来的 created_ts ASC 会先翻最老的账。
    rows = c.execute(
        "SELECT id, text, source, created_ts, ask_count, status FROM followups "
        "WHERE done=0 AND status!='confirming' "
        "AND (last_asked_ts=0 OR last_asked_ts < ?) "
        "AND created_ts > ? AND ask_count < ? "
        "ORDER BY ask_count ASC, created_ts DESC LIMIT ?",
        (cutoff, fresh, ASK_MAX_TIMES, max(limit * 3, limit))).fetchall()
    c.close()
    out = [{"id": r[0], "text": r[1], "source": r[2],
            "created": r[3], "asks": r[4], "status": r[5]}
           for r in rows if askable(r[1])]
    return out[:limit]

def mark_asked(ids: list[int]) -> None:
    if not ids:
        return
    init()
    c = _conn()
    with c:
        c.executemany(
            "UPDATE followups SET last_asked_ts=?, ask_count=ask_count+1 WHERE id=?",
            [(time.time(), i) for i in ids])
    c.close()

def set_status(fid: int, status: str) -> None:
    """改状态。done / cancelled 同时把 done 派生标记置 1。"""
    if status not in STATUSES:
        return
    init()
    c = _conn()
    with c:
        c.execute("UPDATE followups SET status=?, status_ts=?, done=? WHERE id=?",
                  (status, time.time(), 1 if status in CLOSED else 0, fid))
    c.close()

def mark_done(ids: list[int]) -> int:
    if not ids:
        return 0
    init()
    c = _conn()
    with c:
        c.executemany("UPDATE followups SET status='done', status_ts=?, done=1 WHERE id=?",
                      [(time.time(), i) for i in ids])
        n = c.total_changes
    c.close()
    return n

def cancel_ref(ref_id: str) -> int:
    """把「某个来源」派生出来的跟进一起取消。

    目前只有提醒用它：提醒删了，跟进还挂着的话，主动消息过两天会去问
    「你那个喝水的事怎么样了」——提醒根本没发生过，问得莫名其妙。
    """
    rid = str(ref_id or "")
    if not rid:
        return 0
    init()
    c = _conn()
    with c:
        cur = c.execute(
            "UPDATE followups SET status='cancelled', status_ts=?, done=1 "
            "WHERE ref_id=? AND status NOT IN ('done','cancelled')",
            (time.time(), rid))
        n = cur.rowcount
    c.close()
    return n

def last_asked(hours: float = 48) -> dict | None:
    """最近问过、还没关闭的那一条。

    解决「答复太短匹配不上」：主动问「那个 bug 改到哪了」，他回「改完了」，
    两个字和原句重叠不到 0.7，按内容匹配永远关不掉，于是过 5 天又问一遍。
    有「刚问过」这个上下文时，短答复应该归到那一条上。
    """
    init()
    cut = time.time() - hours * 3600
    c = _conn()
    r = c.execute(
        "SELECT id, text FROM followups WHERE done=0 AND status!='confirming'"
        " AND last_asked_ts > ? ORDER BY last_asked_ts DESC LIMIT 1", (cut,)).fetchone()
    c.close()
    return {"id": r[0], "text": r[1]} if r else None

def _candidates(text: str) -> list[tuple[int, str]]:
    init()
    c = _conn()
    rows = c.execute("SELECT id, text FROM followups WHERE done=0").fetchall()
    c.close()
    return [(fid, ftxt) for fid, ftxt in rows if _overlap(ftxt, text) >= 0.7]

def apply(res: dict) -> int:
    """把 resolve() 的结果落库。confirming 的条目会被主动消息跳过，等下一句确认。"""
    if not res:
        return 0
    n = 0
    for fid in res.get("done") or []:
        set_status(fid, "done")
        n += 1
    for fid in (res.get("status") or {}):
        set_status(fid, res["status"][fid])
        n += 1
    for fid in res.get("drop") or []:
        set_status(fid, "cancelled")
        n += 1
    for fid in res.get("confirm") or []:
        set_status(fid, "confirming")
        n += 1
    return n

def confirm_pending(text: str, window_hours: float = 24) -> int:
    """上一轮问了「办完了吗」，这一轮用户说了「对/是/嗯」就关闭。

    只在有 doing 状态、且刚被问过的行上生效，避免把随便一句「是的」当成确认。
    """
    t = (text or "").strip()
    if len(t) > 12 or not any(w in t for w in AFFIRM):
        return 0
    init()
    cutoff = time.time() - window_hours * 3600
    c = _conn()
    rows = c.execute(
        "SELECT id FROM followups WHERE status='confirming' AND done=0 AND status_ts>=?",
        (cutoff,)).fetchall()
    if rows:
        with c:
            c.executemany(
                "UPDATE followups SET status='done', done=1, status_ts=? WHERE id=?",
                [(time.time(), r[0]) for r in rows])
    c.close()
    return len(rows)

def describe_statuses() -> dict:
    init()
    c = _conn()
    rows = c.execute("SELECT status, COUNT(*) FROM followups GROUP BY status").fetchall()
    c.close()
    return {k: v for k, v in rows}

def texts_for(ids: list[int], limit: int = 3) -> list[str]:
    """按 id 取回原文，给聊天提示用（「他好像在说 X 办完了」）。"""
    if not ids:
        return []
    init()
    c = _conn()
    q = ",".join("?" * len(ids))
    rows = c.execute(f"SELECT text FROM followups WHERE id IN ({q})", list(ids)).fetchall()
    c.close()
    return [r[0] for r in rows][:limit]

def expire(max_age_days: float = MAX_AGE_DAYS, max_asks: int = MAX_ASKS,
           keep_max: int = KEEP_MAX_OPEN) -> dict:
    """给待办做减法。三条规则，都是「改成 cancelled」，不删数据。

    1. 太老：建了 60 天还没结果，默认不重要了
    2. 问太多次：问过 6 次还没关闭，说明他不打算做
    3. 积压：未关闭超过 30 条时，从最老、且一次没问过的开始取消
       （问过的留着，说明是我们主动提过的话题）

    为什么要做：不清的话主动消息会永远在问上周甚至上个月的事，
    越问越旧、越问越像催债，反而把「偶尔关心一下」这件事做坏了。
    """
    init()
    now = time.time()
    c = _conn()
    old_cut = now - max_age_days * 86400
    n_old = c.execute(
        "UPDATE followups SET status='cancelled', status_ts=?, done=1 "
        "WHERE done=0 AND created_ts < ?", (now, old_cut)).rowcount
    n_ask = c.execute(
        "UPDATE followups SET status='cancelled', status_ts=?, done=1 "
        "WHERE done=0 AND ask_count >= ?", (now, max_asks)).rowcount
    n_over = 0
    left = c.execute("SELECT COUNT(*) FROM followups WHERE done=0").fetchone()[0]
    if left > keep_max:
        extra = left - keep_max
        rows = c.execute(
            "SELECT id FROM followups WHERE done=0 "
            "ORDER BY ask_count ASC, created_ts ASC LIMIT ?", (extra,)).fetchall()
        if rows:
            c.executemany(
                "UPDATE followups SET status='cancelled', status_ts=?, done=1 WHERE id=?",
                [(now, r[0]) for r in rows])
            n_over = len(rows)
    c.commit()
    c.close()
    return {"old": n_old, "asked": n_ask, "overflow": n_over,
            "cancelled": n_old + n_ask + n_over}

def pending(limit: int = 10) -> list[dict]:
    """最近、还值得提的未关闭事项（给聊天 prompt 用）。

    以前只要 done=0 就塞进 prompt，机器人会在正常聊天里突然提
    10 天前的旧账。这里和主动询问用同一道「新鲜 + 像他的待办」判据。
    """
    init()
    fresh = time.time() - ASK_MAX_AGE_DAYS * 86400
    c = _conn()
    rows = c.execute(
        "SELECT id, text, source, ask_count, status FROM followups "
        "WHERE done=0 AND created_ts > ? ORDER BY created_ts DESC LIMIT ?",
        (fresh, max(limit * 3, limit))).fetchall()
    c.close()
    out = [{"id": r[0], "text": r[1], "source": r[2], "asks": r[3], "status": r[4]}
           for r in rows if askable(r[1])]
    return out[:limit]

def stats() -> dict:
    init()
    c = _conn()
    total = c.execute("SELECT COUNT(*) FROM followups").fetchone()[0]
    open_ = c.execute("SELECT COUNT(*) FROM followups WHERE done=0").fetchone()[0]
    c.close()
    return {"total": total, "open": open_, "done": total - open_}


def resolve(text: str) -> dict:
    """查候选/最近问过，再交给纯函数 resolve_from。"""
    t = (text or "").strip()
    if len(t) < 2:
        return {"done": [], "confirm": [], "status": {}, "drop": []}
    cands = _candidates(t)
    la = last_asked() if not cands else None
    return resolve_from(t, cands, la)
