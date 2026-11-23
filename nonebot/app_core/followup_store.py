"""[orchestration] followup 的 SQLite 读写；DB 路径在模块内定义（测试直接补本模块）。"""
from __future__ import annotations

import logging
import sqlite3
import time

from app_core import paths as _paths
from app_core import textsim
from app_core.followup_domain import (  # noqa: F401
    ANSWER_COOLDOWN_DAYS, ASK_MAX_AGE_DAYS, ASK_MAX_TIMES, CLOSED, COOLDOWN_DAYS,
    IGNORE_LIMIT, KEEP_MAX_OPEN, AFFIRM, MAX_AGE_DAYS, MAX_ASKS, MAX_TEXT,
    NEAR_DUP, STATUSES, _overlap, askable, expired_one_off, fresh_basis,
    is_affirm, links_question, resolve_from, responds_to,
)

log = logging.getLogger("qqbot.followup")

DB = _paths.DATA / "chat.db"


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB, timeout=15)
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
            "ALTER TABLE followups ADD COLUMN answered_ts REAL NOT NULL DEFAULT 0",
            "ALTER TABLE followups ADD COLUMN ignored INTEGER NOT NULL DEFAULT 0",
            # 这件事是「什么时候说的」：created_ts 是入库时间，延迟总结时两者
            # 能差几天，主动消息里的「约 N 天前他说过」要用 source_ts。
            "ALTER TABLE followups ADD COLUMN source_ts REAL NOT NULL DEFAULT 0",
            "ALTER TABLE followups ADD COLUMN asked_msg_id INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE followups ADD COLUMN asked_text TEXT NOT NULL DEFAULT ''",
        ):
            try:
                c.execute(ddl)
            except Exception:
                pass
        c.execute("UPDATE followups SET status='done' "
                  "WHERE done=1 AND status='todo'")
    c.close()

def add(text: str, source: str = "memory", ref_id: str = "",
        source_ts: float | None = None) -> bool:
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

            # 未关闭的 + 最近 30 天关闭的都参与去重：
            # 只比未关闭的，会让「已经说过的事」换个说法重新进来再问一遍。
            rows = c.execute(
                "SELECT text FROM followups WHERE (done=0 AND status!='cancelled') "
                "OR created_ts > ?", (time.time() - 30 * 86400,)).fetchall()
            for (old,) in rows:
                if similar(old, t) >= NEAR_DUP:
                    log.debug("跟进近似重复，跳过：%s ≈ %s", t[:20], old[:20])
                    return False
        except Exception:  # noqa: BLE001
            pass                                  # 相似度算不出来也不影响写入
        cur = c.execute(
            "INSERT OR IGNORE INTO followups(text, source, ref_id, created_ts, source_ts) "
            "VALUES (?,?,?,?,?)",
            (t, source, str(ref_id), time.time(), float(source_ts or 0)))
        c.commit()
        # 用 rowcount 而不是 total_changes：后者是连接级累计值，判断不准
        return cur.rowcount > 0
    except Exception:
        return False
    finally:
        c.close()

def add_many(items: list[str], source: str = "memory", ref_id: str = "",
             source_ts: float | None = None) -> int:
    n = 0
    for it in items or []:
        if add(it, source, ref_id, source_ts=source_ts):
            n += 1
    return n

def _mentionable(text: str, created: float, source_ts: float, now: float) -> bool:
    """主动选题和普通聊天注入共用：过期的一次性计划、补录的旧事都不算还能问。"""
    if expired_one_off(text, now):
        return False
    return fresh_basis(created, source_ts) > now - ASK_MAX_AGE_DAYS * 86400


def due_for_asking(limit: int = 5, cooldown_days: float = COOLDOWN_DAYS) -> list[dict]:
    """还没关闭、且距上次追问够久的事项。优先「从没问过」的。"""
    init()
    now = time.time()
    cutoff = now - cooldown_days * 86400
    c = _conn()
    # 多取一些再按语义过滤：过滤在 Python 侧，SQL 先按时间/次数/冷却筛。
    # 排序改成「没问过的优先，新的优先」——原来的 created_ts ASC 会先翻最老的账。
    answer_cutoff = now - ANSWER_COOLDOWN_DAYS * 86400
    rows = c.execute(
        "SELECT id, text, source, created_ts, ask_count, status, source_ts "
        "FROM followups "
        "WHERE done=0 AND status NOT IN ('confirming','postponed') "
        "AND (last_asked_ts=0 OR last_asked_ts < ?) "
        "AND ask_count < ? "
        "AND (answered_ts=0 OR last_asked_ts > answered_ts OR answered_ts < ?) "
        "ORDER BY ask_count ASC, created_ts DESC LIMIT ?",
        (cutoff, ASK_MAX_TIMES, answer_cutoff,
         max(limit * 6, limit))).fetchall()
    c.close()
    out = []
    for r in rows:
        if not askable(r[1]):
            continue
        src = float(r[6] or 0)
        if not _mentionable(r[1], float(r[3] or 0), src, now):
            continue
        out.append({"id": r[0], "text": r[1], "source": r[2],
                    "created": r[3], "asks": r[4], "status": r[5],
                    "source_ts": src})
    return out[:limit]

def mark_asked(ids: list[int], msg_id: int = 0, asked_text: str = "") -> None:
    if not ids:
        return
    init()
    now = time.time()
    c = _conn()
    with c:
        c.executemany(
            "UPDATE followups SET last_asked_ts=?, ask_count=ask_count+1 WHERE id=?",
            [(now, i) for i in ids])
        if msg_id or asked_text:
            c.executemany(
                "UPDATE followups SET asked_msg_id=?, asked_text=? WHERE id=?",
                [(int(msg_id or 0), (asked_text or "")[:MAX_TEXT], i) for i in ids])
    c.close()

def mark_answered(ids: list[int]) -> None:
    """他回应了这件事（不一定是办完）。只记时间，不改状态。

    为什么需要单独记：原来的状态机只有 todo/doing/done，他答一句「在弄」
    匹配不上任何信号词，条目就一直停在 todo，冷却 5 天后又问一遍。
    """
    if not ids:
        return
    init()
    c = _conn()
    with c:
        c.executemany(
            "UPDATE followups SET answered_ts=? WHERE id=?",
            [(time.time(), i) for i in ids])
        c.executemany(
            "UPDATE followups SET status='todo', status_ts=? "
            "WHERE id=? AND status='postponed'",
            [(time.time(), i) for i in ids])
    c.close()


def mark_ignored(ids: list[int], conn: sqlite3.Connection | None = None) -> None:
    """问了没回应：+1；到 IGNORE_LIMIT 就转 postponed，不再主动问。

    `conn` 可传入一个已开事务的连接（主动消息用它把"这条发送已结算"和
    "这条跟进 +1 没理"放进同一个事务，避免结算了却没加计数）。
    """
    if not ids:
        return
    if conn is None:
        init()
    c = conn or _conn()
    try:
        with c:
            c.executemany("UPDATE followups SET ignored=ignored+1 WHERE id=?",
                          [(i,) for i in ids])
            c.executemany(
                "UPDATE followups SET status='postponed', status_ts=? "
                "WHERE id=? AND ignored>=?",
                [(time.time(), i, IGNORE_LIMIT) for i in ids])
    finally:
        if conn is None:
            c.close()


def set_status(fid: int, status: str) -> None:
    """改状态。done / cancelled 同时把 done 派生标记置 1。

    **状态更新和回应时间一起落库**：他这次确实回应了（哪怕是"做了一半"），
    只改状态不记 answered_ts，14 天冷却的判据就看不到这次回应。
    """
    if status not in STATUSES:
        return
    init()
    c = _conn()
    now = time.time()
    with c:
        c.execute("UPDATE followups SET status=?, status_ts=?, answered_ts=?, "
                  "done=? WHERE id=?",
                  (status, now, now, 1 if status in CLOSED else 0, fid))
    c.close()

def mark_done(ids: list[int]) -> int:
    if not ids:
        return 0
    init()
    c = _conn()
    with c:
        c.executemany("UPDATE followups SET status='done', status_ts=?, answered_ts=?, "
                      "done=1 WHERE id=?",
                      [(time.time(), time.time(), i) for i in ids])
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
    """上一轮问了「办完了吗」，这一轮整句是「对/是/嗯」才关掉**这一条**。

    「不是」里含「是」，不能当成肯定。两条同时待确认时，一句「嗯」只关最近那条。
    """
    if not is_affirm(text):
        return 0
    init()
    cutoff = time.time() - window_hours * 3600
    c = _conn()
    row = c.execute(
        "SELECT id FROM followups WHERE status='confirming' AND done=0 AND status_ts>=? "
        "ORDER BY status_ts DESC LIMIT 1",
        (cutoff,)).fetchone()
    if row:
        now = time.time()
        with c:
            c.execute(
                "UPDATE followups SET status='done', done=1, status_ts=?, answered_ts=? "
                "WHERE id=?",
                (now, now, row[0]))
    c.close()
    return 1 if row else 0

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
    """还值得主动提起的未关闭事项。已回应、已搁置、过期计划不在这里。"""
    init()
    now = time.time()
    c = _conn()
    rows = c.execute(
        "SELECT id, text, source, ask_count, status, created_ts, source_ts, "
        "answered_ts FROM followups "
        "WHERE done=0 AND status NOT IN ('postponed','confirming') "
        "ORDER BY created_ts DESC LIMIT ?",
        (max(limit * 6, limit),)).fetchall()
    c.close()
    out = []
    for r in rows:
        if not askable(r[1]):
            continue
        if float(r[7] or 0) > 0:
            continue
        if not _mentionable(r[1], float(r[5] or 0), float(r[6] or 0), now):
            continue
        out.append({"id": r[0], "text": r[1], "source": r[2], "asks": r[3],
                    "status": r[4]})
    return out[:limit]


def for_prompt(user_text: str, limit: int = 3) -> list[str]:
    """普通回复注入：能问的只在聊到相关词时给；已回应/已搁置的带上状态，不当成没问过。"""
    t = user_text or ""
    lines = [x["text"][:40] for x in pending(limit * 2)
             if _overlap(x.get("text") or "", t) >= 0.2]
    init()
    c = _conn()
    rows = c.execute(
        "SELECT text, status, answered_ts FROM followups "
        "WHERE done=0 AND (status='postponed' OR answered_ts>0) "
        "ORDER BY answered_ts DESC, status_ts DESC LIMIT 20").fetchall()
    c.close()
    for text, status, answered in rows:
        if _overlap(text or "", t) < 0.2:
            continue
        label = "已搁置" if status == "postponed" else "已回应"
        lines.append(f"{(text or '')[:40]}（{label}，别当没问过）")
        if len(lines) >= limit:
            break
    return lines[:limit]

def id_for_text(text: str) -> int | None:
    """按文本找最新一条（记忆契约创建后要返回 ref）。

    近似重复在 add 里已被拒，所以这里按文本查最新是安全的。
    """
    init()
    c = _conn()
    try:
        row = c.execute(
            "SELECT id FROM followups WHERE text=? ORDER BY id DESC LIMIT 1",
            (text,)).fetchone()
        return int(row[0]) if row else None
    finally:
        c.close()


def stats() -> dict:
    init()
    c = _conn()
    total = c.execute("SELECT COUNT(*) FROM followups").fetchone()[0]
    open_ = c.execute("SELECT COUNT(*) FROM followups WHERE done=0").fetchone()[0]
    c.close()
    return {"total": total, "open": open_, "done": total - open_}


def note_asked(text: str, msg_id: int = 0) -> list[int]:
    """普通回复或主动消息里问到的跟进，记成已问。同一句两分钟内不重复计数。"""
    asked = (text or "").strip()
    if not asked:
        return []
    init()
    now = time.time()
    c = _conn()
    rows = c.execute(
        "SELECT id, text, last_asked_ts, asked_text FROM followups "
        "WHERE done=0 AND status NOT IN ('done','cancelled')").fetchall()
    c.close()
    ids = []
    for fid, item, last, prev in rows:
        if not links_question(item or "", asked):
            continue
        if (float(last or 0) and now - float(last) < 120
                and (prev or "") == asked[:MAX_TEXT]):
            continue
        ids.append(int(fid))
    mark_asked(ids, msg_id=msg_id, asked_text=asked)
    return ids


def last_sent_assistant(session: str) -> str:
    """紧邻的上一条实际发出的助手消息。跟进表和聊天库是同一个 chat.db。"""
    if not session:
        return ""
    init()
    c = _conn()
    try:
        row = c.execute(
            "SELECT content FROM messages WHERE session=? AND role='assistant' "
            "ORDER BY id DESC LIMIT 1", (session,)).fetchone()
    except Exception:  # noqa: BLE001
        row = None
    finally:
        c.close()
    return (row[0] if row else "") or ""


def attribute_reply(user_text: str, session: str = "",
                    reply_msg_id: int = 0,
                    last_assistant: str | None = None) -> dict | None:
    """这句是在接哪条提问。先引用消息 id，再看紧邻的上一条实际发送。"""
    t = (user_text or "").strip()
    if len(t) < 1:
        return None
    init()
    c = _conn()
    item = None
    if reply_msg_id:
        row = c.execute(
            "SELECT id, text, asked_text FROM followups "
            "WHERE done=0 AND asked_msg_id=? ORDER BY last_asked_ts DESC LIMIT 1",
            (int(reply_msg_id),)).fetchone()
        if row:
            item = {"id": row[0], "text": row[1], "asked_text": row[2] or ""}
    if item is None:
        asked = last_assistant if last_assistant is not None else last_sent_assistant(session)
        if asked:
            rows = c.execute(
                "SELECT id, text, asked_text, last_asked_ts FROM followups "
                "WHERE done=0 AND last_asked_ts>0 "
                "ORDER BY last_asked_ts DESC LIMIT 8").fetchall()
            for fid, text, q, _ts in rows:
                if links_question(text or "", asked) or (q and asked[:80] in q or q[:80] in asked):
                    item = {"id": fid, "text": text, "asked_text": q or asked}
                    break
    c.close()
    if not item:
        return None
    if not responds_to(item["text"], item.get("asked_text") or "", t):
        return None
    return item


def still_due(fid: int) -> bool:
    """生成期间事项可能已被回答。发送前再查一次。"""
    if not fid:
        return False
    return any(int(x["id"]) == int(fid) for x in due_for_asking(50))


def resolve(text: str) -> dict:
    """查候选/最近问过，再交给纯函数 resolve_from。"""
    t = (text or "").strip()
    if len(t) < 2:
        return {"done": [], "confirm": [], "status": {}, "drop": []}
    cands = _candidates(t)
    la = last_asked() if not cands else None
    return resolve_from(t, cands, la)
