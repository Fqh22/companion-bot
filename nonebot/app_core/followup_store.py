"""[orchestration] followup 的 SQLite 读写；DB 路径在模块内定义（测试直接补本模块）。"""
from __future__ import annotations

from contextlib import closing
import logging
import sqlite3
import time

from app_core import paths as _paths
from app_core import textsim, timenorm
from app_core.followup_domain import (  # noqa: F401
    ANSWER_COOLDOWN_DAYS, ASK_MAX_AGE_DAYS, ASK_MAX_TIMES, CLOSED, COOLDOWN_DAYS,
    IGNORE_LIMIT, KEEP_MAX_OPEN, AFFIRM, MAX_AGE_DAYS, MAX_ASKS, MAX_TEXT,
    NEAR_DUP, STATUSES, _overlap, askable, expired_one_off, fresh_basis,
    QuietControl, control_target, is_affirm, links_question, quiet_control, resolve_from, responds_to, shared_span, is_negate, snooze_deadline, strip_punct,
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
            "ALTER TABLE followups ADD COLUMN quiet_until REAL NOT NULL DEFAULT 0",
            "ALTER TABLE followups ADD COLUMN source_msg_id INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE followups ADD COLUMN source_key TEXT NOT NULL DEFAULT ''",
        ):
            try:
                c.execute(ddl)
            except Exception:
                pass
        c.execute(
            "CREATE TABLE IF NOT EXISTS delivery_refs ("
            "session TEXT NOT NULL, platform_ref TEXT NOT NULL, "
            "assistant_msg_id INTEGER NOT NULL, text TEXT NOT NULL, "
            "PRIMARY KEY(session, platform_ref))")
        c.execute(
            "CREATE TABLE IF NOT EXISTS followup_clarifications ("
            "session TEXT PRIMARY KEY, action TEXT NOT NULL, until_ts REAL NOT NULL, "
            "expires_ts REAL NOT NULL, target_id INTEGER NOT NULL DEFAULT 0)")
        if not any(r[1] == "target_id" for r in c.execute("PRAGMA table_info(followup_clarifications)")):
            c.execute("ALTER TABLE followup_clarifications ADD COLUMN target_id INTEGER NOT NULL DEFAULT 0")
        c.execute("CREATE INDEX IF NOT EXISTS idx_delivery_refs_message ON delivery_refs(assistant_msg_id)")
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_followup_source_key ON followups(source_key) "
                  "WHERE source_key!=''")
        # 历史清理脚本各自连接 SQLite，触发器让引用跟随历史删除。
        if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='messages'").fetchone():
            c.execute(
                "CREATE TRIGGER IF NOT EXISTS delete_delivery_refs AFTER DELETE ON messages "
                "BEGIN DELETE FROM delivery_refs WHERE assistant_msg_id=OLD.id; END")
        c.execute("UPDATE followups SET status='done' "
                  "WHERE done=1 AND status='todo'")
    c.close()

def add(text: str, source: str = "memory", ref_id: str = "",
        source_ts: float | None = None, *, source_msg_id: int = 0,
        source_key: str = "") -> bool:
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
        c.execute("BEGIN IMMEDIATE")
        if source_msg_id:
            # 同一条用户承诺被后台重放时保留原状态，含已答、暂停和关闭。
            # 允许一条消息里明确交代多件独立事项。
            source_tasks = c.execute("SELECT text FROM followups WHERE source_msg_id=?",
                                     (source_msg_id,)).fetchall()
            if any(t in old or old in t for (old,) in source_tasks):
                return False
        try:
            from app_core.textsim import similar

            # 未关闭的 + 最近 30 天关闭的都参与去重：
            # 只比未关闭的，会让「已经说过的事」换个说法重新进来再问一遍。
            rows = c.execute(
                "SELECT text FROM followups WHERE (done=0 AND status!='cancelled') "
                "OR created_ts > ? OR quiet_until < 0 OR (source_msg_id>0 AND source_msg_id=?)",
                (time.time() - 30 * 86400, source_msg_id)).fetchall()
            for (old,) in rows:
                if similar(old, t) >= NEAR_DUP:
                    log.debug("跟进近似重复，跳过：%s ≈ %s", t[:20], old[:20])
                    return False
        except Exception:  # noqa: BLE001
            pass                                  # 相似度算不出来也不影响写入
        cur = c.execute(
            "INSERT OR IGNORE INTO followups(text, source, ref_id, created_ts, source_ts, source_msg_id, source_key) "
            "VALUES (?,?,?,?,?,?,?)",
            (t, source, str(ref_id), time.time(), float(source_ts or 0), source_msg_id, source_key))
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
        "AND quiet_until>=0 AND quiet_until<=? AND ask_count < ? "
        "AND (answered_ts=0 OR answered_ts < ?) "
        "ORDER BY ask_count ASC, created_ts DESC LIMIT ?",
        (cutoff, now, ASK_MAX_TIMES, answer_cutoff,
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
    with closing(_conn()) as c, c:
        _mark_asked(c, ids, msg_id, asked_text, now)


def _mark_asked(c, ids: list[int], msg_id: int, text: str, now: float) -> None:
    c.executemany(
        "UPDATE followups SET last_asked_ts=?, ask_count=ask_count+1 WHERE id=? AND done=0",
        [(now, fid) for fid in ids])
    if msg_id or text:
        c.executemany(
            "UPDATE followups SET asked_msg_id=?, asked_text=? WHERE id=? AND done=0",
            [(msg_id, text[:MAX_TEXT], fid) for fid in ids])


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
            now = time.time()
            c.executemany(
                "UPDATE followups SET ignored=ignored+1 WHERE id=? AND done=0 "
                "AND quiet_until>=0 AND quiet_until<=?",
                [(i, now) for i in ids])
            c.executemany(
                "UPDATE followups SET status='postponed', status_ts=? "
                "WHERE id=? AND done=0 AND quiet_until>=0 AND quiet_until<=? AND ignored>=?",
                [(now, i, now, IGNORE_LIMIT) for i in ids])
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

def confirm_pending(text: str, window_hours: float = 24, session: str = "",
                    last_assistant: str | None = None) -> int:
    """整句肯定才关，而且必须是在接这条待确认的提问。

    「好」另起话题不能关。两条同时待确认时，只关对得上的那一条。
    """
    if not is_affirm(text):
        return 0
    init()
    asked = last_assistant if last_assistant is not None else last_sent_assistant(session)
    if not asked:
        return 0
    cutoff = time.time() - window_hours * 3600
    c = _conn()
    rows = c.execute(
        "SELECT id, text, asked_text FROM followups "
        "WHERE status='confirming' AND done=0 AND status_ts>=? "
        "ORDER BY status_ts DESC LIMIT 8",
        (cutoff,)).fetchall()
    c.close()
    target = None
    for fid, item, q in rows:
        blob = f"{item or ''} {q or ''}"
        if links_question(item or "", asked) or shared_span(blob, asked, 2):
            target = fid
            break
    if target is None:
        return 0
    now = time.time()
    c = _conn()
    with c:
        c.execute(
            "UPDATE followups SET status='done', done=1, status_ts=?, answered_ts=? "
            "WHERE id=?",
            (now, now, target))
    c.close()
    return 1

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
        "WHERE done=0 AND quiet_until>=0 AND quiet_until<=? AND created_ts < ?",
        (now, now, old_cut)).rowcount
    n_ask = c.execute(
        "UPDATE followups SET status='cancelled', status_ts=?, done=1 "
        "WHERE done=0 AND quiet_until>=0 AND quiet_until<=? AND ask_count >= ?",
        (now, now, max_asks)).rowcount
    n_over = 0
    c.execute("DELETE FROM followup_clarifications WHERE expires_ts<=?", (now,))
    left = c.execute(
        "SELECT COUNT(*) FROM followups WHERE done=0 AND quiet_until>=0 AND quiet_until<=?",
        (now,)).fetchone()[0]
    if left > keep_max:
        extra = left - keep_max
        rows = c.execute(
            "SELECT id FROM followups WHERE done=0 AND quiet_until>=0 AND quiet_until<=? "
            "ORDER BY ask_count ASC, created_ts ASC LIMIT ?", (now, extra)).fetchall()
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
        "AND quiet_until>=0 AND quiet_until<=? ORDER BY created_ts DESC LIMIT ?",
        (now, max(limit * 6, limit))).fetchall()
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
             if shared_span(x.get("text") or "", t)]
    init()
    c = _conn()
    rows = c.execute(
        "SELECT text, status, answered_ts FROM followups "
        "WHERE done=0 AND quiet_until>=0 AND quiet_until<=? "
        "AND (status='postponed' OR answered_ts>0) "
        "ORDER BY answered_ts DESC, status_ts DESC LIMIT 20", (time.time(),)).fetchall()
    c.close()
    for text, status, answered in rows:
        if not shared_span(text or "", t):
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
    with closing(_conn()) as c, c:
        return _note_asked(c, asked, msg_id, now)


def _note_asked(c, asked: str, msg_id: int, now: float) -> list[int]:
    rows = c.execute(
        "SELECT id, text, last_asked_ts, asked_text FROM followups "
        "WHERE done=0 AND status NOT IN ('done','cancelled') "
        "AND quiet_until>=0 AND quiet_until<=?", (now,)).fetchall()
    ids = [fid for fid, text, last, prev in rows
           if links_question(text, asked)
           and not (last and now - last < 120 and prev == asked[:MAX_TEXT])]
    _mark_asked(c, ids, msg_id, asked, now)
    return ids


def record_delivery(db, session: str, text: str, *, track_followup: bool,
                    references: tuple[tuple[str, str], ...] = (),
                    topic_key: str = "", topic: str = "") -> int:
    """将已发送的正文、引用、跟进及主动日志写进同一个 SQLite 事务。"""
    references = tuple(dict(references).items())
    with closing(sqlite3.connect(db, timeout=15)) as c, c:
        # 先拿写锁，让重复平台回执的检查和插入串行。
        c.execute("BEGIN IMMEDIATE")
        for reference, _ in references:
            existing = c.execute(
                "SELECT assistant_msg_id FROM delivery_refs WHERE session=? AND platform_ref=?",
                (session, reference)).fetchone()
            if existing:
                return int(existing[0])
        now = time.time()
        msg_id = c.execute(
            "INSERT INTO messages(session, role, content, ts) VALUES (?,'assistant',?,?)",
            (session, text, now)).lastrowid
        c.executemany(
            "INSERT INTO delivery_refs(session, platform_ref, assistant_msg_id, text) VALUES (?,?,?,?)",
            [(session, reference, msg_id, segment) for reference, segment in references])
        if topic_key:
            c.execute(
                "INSERT INTO proactive_log(ts,topic_key,topic,text,session,assistant_msg_id,platform_ref) "
                "VALUES(?,?,?,?,?,?,?)",
                (now, topic_key, topic, text, session, msg_id, references[0][0] if references else ""))
            _, _, fid = topic_key.partition(":")
            if topic_key.startswith("fu:") and fid.isdigit():
                _mark_asked(c, [int(fid)], msg_id, text, now)
        elif track_followup:
            _note_asked(c, text, msg_id, now)
        return int(msg_id)


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


def _reply_context(c, session: str, user_msg_id: int):
    if not session:
        return None
    if user_msg_id:
        cutoff = user_msg_id
    else:
        cutoff = 2**63 - 1
    return c.execute(
        "SELECT id, content, ts, role FROM messages WHERE session=? AND id<? "
        "ORDER BY id DESC LIMIT 1", (session, cutoff)).fetchone()


def attribute_reply(user_text: str, session: str = "",
                    reply_msg_id: int = 0,
                    last_assistant: str | None = None,
                    user_msg_id: int = 0, platform_ref: str | None = None,
                    *, require_answer: bool = True) -> dict | None:
    """先找唯一的提问对象，再判用户是否在回答。"""
    init()
    c = _conn()
    try:
        if platform_ref and _has_proactive_receipts(c):
            sent = c.execute(
                "SELECT p.id, p.text, f.id, f.text, f.status, f.done FROM proactive_log p "
                "JOIN followups f ON p.topic_key='fu:' || f.id "
                "WHERE p.session=? AND p.platform_ref=?",
                (session, platform_ref)).fetchall()
            if sent:
                if len(sent) != 1:
                    return None
                pid, question, fid, text, status, done = sent[0]
                if done or (require_answer and not responds_to(text, question, user_text)):
                    return None
                return {"id": fid, "text": text, "asked_text": question, "status": status,
                        "assistant_msg_id": 0, "proactive_id": pid}
        if platform_ref:
            recorded = c.execute(
                "SELECT r.text FROM delivery_refs r JOIN messages m ON m.id=r.assistant_msg_id "
                "WHERE r.session=? AND r.platform_ref=? AND m.session=r.session",
                (session, platform_ref)).fetchone()
            if recorded:
                last_assistant = recorded[0]
        if platform_ref and last_assistant is None:
            return None
        adjacent = _reply_context(c, session, user_msg_id) if last_assistant is None else None
        if adjacent and (adjacent[3] != "assistant" or time.time() - adjacent[2] > 7200):
            adjacent = None
        asked = last_assistant if last_assistant is not None else (adjacent[1] if adjacent else "")
        rows = c.execute(
            "SELECT id, text, asked_text, status, asked_msg_id FROM followups "
            "WHERE done=0 AND last_asked_ts>0").fetchall()
        exact = [r for r in rows if (reply_msg_id and r[4] == reply_msg_id)
                 or (asked and r[2] == asked[:MAX_TEXT])]
        matches = exact or [r for r in rows if links_question(r[1], asked)]
        if len(matches) != 1:
            return None
        fid, text, recorded, status, _ = matches[0]
        question = asked or recorded
        if require_answer and not responds_to(text, question, user_text):
            return None
        return {"id": fid, "text": text, "asked_text": question, "status": status,
                "assistant_msg_id": adjacent[0] if adjacent else 0}
    finally:
        c.close()


_CLARIFY = "你指哪件事？回我事项名就行。"
_CLARIFY_DATE = "暂停到哪天？可以说「三天」或给个具体日期。"


def _named_control(c, target: str) -> tuple[int, str] | None:
    if len(target) < 2:
        return None
    rows = c.execute(
        "SELECT id,text FROM followups WHERE done=0 AND instr(text,?)>0",
        (target,)).fetchall()
    exact = [row for row in rows if row[1] == target]
    matches = exact or rows
    return matches[0] if len(matches) == 1 else None


def control_turn(text: str, session: str, user_msg_id: int = 0,
                 quote_text: str | None = None, reply_to: str | None = None,
                 *, suggested: QuietControl | None = None,
                 expected_question: str = "") -> str | None:
    """处理主人明确的跟进指令。认不出对象时只确认一次。"""
    now = time.time()
    direct = quiet_control(text, now)
    command = direct or suggested
    hit = None
    if command and (quote_text or reply_to or not command.target):
        hit = attribute_reply(text, session=session, last_assistant=quote_text,
                              user_msg_id=user_msg_id, platform_ref=reply_to,
                              require_answer=False)
        if not direct and hit and hit["asked_text"] != expected_question:
            hit = None
    with closing(_conn()) as c, c:
        pending = c.execute(
            "SELECT action,until_ts,expires_ts,target_id FROM followup_clarifications WHERE session=?",
            (session,)).fetchone()
        if pending:
            c.execute("DELETE FROM followup_clarifications WHERE session=?", (session,))
        if command:
            item = _named_control(c, command.target)
            if hit and (not command.target or (item and item[0] == hit["id"])):
                item = (hit["id"], hit["text"])
            elif hit or ((reply_to or quote_text) and not hit):
                item = None
            if not item:
                c.execute(
                    "INSERT INTO followup_clarifications(session,action,until_ts,expires_ts) "
                    "VALUES(?,?,?,?)", (session, command.action, command.until, now + 600))
                return _CLARIFY
        else:
            if not pending or pending[2] <= now:
                return None
            previous = _reply_context(c, session, user_msg_id)
            if not previous or previous[3] != "assistant":
                return None
            if pending[3] and previous[1] == _CLARIFY_DATE:
                until = snooze_deadline(text, now)
                if not until:
                    return None
                item = c.execute(
                    "SELECT id,text FROM followups WHERE id=? AND done=0", (pending[3],)).fetchone()
                command = QuietControl(pending[0], "", until)
            elif not pending[3] and previous[1] == _CLARIFY:
                item = _named_control(c, control_target(text))
                command = QuietControl(pending[0], "", pending[1])
            else:
                return None
            if not item:
                return None

        fid, subject = item
        if command.action == "snooze" and not command.until:
            c.execute(
                "INSERT INTO followup_clarifications(session,action,until_ts,expires_ts,target_id) "
                "VALUES(?,?,?,?,?)", (session, command.action, 0, now + 600, fid))
            return _CLARIFY_DATE
        if command.action == "snooze" and command.until <= now:
            return "刚才指定的暂停时间已经过了。"
        if command.action == "cancel":
            updated = c.execute(
                "UPDATE followups SET quiet_until=-1,status='cancelled',status_ts=?,done=1 "
                "WHERE id=? AND done=0", (now, fid))
        elif command.action == "resume":
            updated = c.execute(
                "UPDATE followups SET quiet_until=0,ignored=0,"
                "status=CASE WHEN status='postponed' THEN 'todo' ELSE status END "
                "WHERE id=? AND done=0", (fid,))
        else:
            updated = c.execute(
                "UPDATE followups SET quiet_until=? WHERE id=? AND done=0",
                (command.until, fid))
        if not updated.rowcount:
            return "这件事已经结束跟进了。"
        if hit and hit["id"] == fid:
            _link_proactive_answer(c, fid, session, hit, now)

    if command.action == "cancel":
        return f"好，「{subject}」就跟进到这里。"
    if command.action == "resume":
        return f"好，「{subject}」已解除暂停。"
    if command.action == "hold":
        return f"好，「{subject}」先放着，等你自己提。"
    until = timenorm.from_ts(command.until)
    return f"好，「{subject}」在 {until:%m月%d日 %H:%M} 前先不问。"


def mentions_open_item(text: str) -> bool:
    with closing(_conn()) as c:
        rows = c.execute("SELECT text FROM followups WHERE done=0").fetchall()
    return any(shared_span(row[0], text) for row in rows)


def quiet_topics(limit: int = KEEP_MAX_OPEN) -> list[str]:
    """给普通回复同一条禁问规则，含用户主动结束的事项。"""
    with closing(_conn()) as c:
        return [r[0] for r in c.execute(
            "SELECT text FROM followups WHERE quiet_until<0 OR quiet_until>? "
            "ORDER BY id DESC LIMIT ?", (time.time(), limit))]


def answer_turn(text: str, session: str, user_msg_id: int = 0,
                quote_text: str | None = None, reply_to: str | None = None,
                *, report: str = "", expected_question: str = "") -> dict:
    """一个用户回合只更新一个事项。引用失败时保留原状态。"""
    text = text.strip().removeprefix("[语音]").strip()
    hit = attribute_reply(text, session=session, last_assistant=quote_text,
                          user_msg_id=user_msg_id, platform_ref=reply_to,
                          require_answer=not bool(report))
    if report and (not hit or not expected_question or hit["asked_text"] != expected_question):
        return {}
    if hit:
        fid = hit["id"]
        normalized = strip_punct(text)
        if report == "completed" and not is_negate(normalized):
            res = {"done": [fid]}
        elif report:
            res = {"touched": [fid]}
        elif (hit["status"] == "confirming" and normalized in
                ("对", "是", "是的", "没错", "对的", "确实")):
            res = {"done": [fid]}
        elif is_negate(normalized):
            res = {"touched": [fid]}
        else:
            res = resolve_from(normalized, [], last_asked=hit, attributed=True)
    elif quote_text or reply_to:
        return {}
    else:
        candidates = _candidates(text)
        if (len(candidates) != 1 or not shared_span(candidates[0][1], text, 3)
                or not responds_to(candidates[0][1], "", text)):
            return {}
        fid = candidates[0][0]
        res = resolve_from(text, candidates)

    status = (res.get("status") or {}).get(fid)
    for key, state in (("done", "done"), ("drop", "cancelled")):
        if fid in (res.get(key) or []):
            status = state
    if fid in (res.get("confirm") or []):
        # 用户已经给过答复，状态不确定时保留原状。
        res["confirm"] = []
    if not status:
        res["touched"] = [fid]

    c = _conn()
    try:
        with c:
            now = time.time()
            if user_msg_id:
                row = c.execute(
                    "SELECT ts FROM messages WHERE id=? AND session=? AND role='user'",
                    (user_msg_id, session)).fetchone()
                if row:
                    now = float(row[0])
            c.execute(
                "UPDATE followups SET answered_ts=MAX(answered_ts, ?) WHERE id=? AND done=0",
                (now, fid))
            if status:
                c.execute(
                    "UPDATE followups SET status=?, status_ts=?, done=? WHERE id=? AND done=0",
                    (status, now, int(status in CLOSED), fid))
            if hit:
                _link_proactive_answer(c, fid, session, hit, now)
    finally:
        c.close()
    return res


def _has_proactive_receipts(c) -> bool:
    return any(row[1] == "platform_ref" for row in c.execute("PRAGMA table_info(proactive_log)"))


def _link_proactive_answer(c, fid: int, session: str, hit: dict, answered_at: float) -> None:
    if not _has_proactive_receipts(c):
        return
    if hit.get("proactive_id"):
        c.execute("UPDATE proactive_log SET answered_ts=? WHERE id=? AND answered_ts=0",
                  (answered_at, hit["proactive_id"]))
        return
    query = ("SELECT id FROM proactive_log WHERE topic_key=? AND session=? "
             "AND ts<=? AND ts>=? AND text=?")
    params = [f"fu:{fid}", session, answered_at, answered_at - 7 * 86400,
              hit["asked_text"]]
    if hit["assistant_msg_id"]:
        query += " AND assistant_msg_id=?"
        params.append(hit["assistant_msg_id"])
    rows = c.execute(query + " LIMIT 2", params).fetchall()
    if len(rows) == 1:
        c.execute("UPDATE proactive_log SET answered_ts=? WHERE id=? AND answered_ts=0",
                  (answered_at, rows[0][0]))


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
