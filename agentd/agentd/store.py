"""SQLite 存储：tasks / steps / events / checkpoints / llm_calls / outbox。

单进程串行执行，用同步 sqlite3 + 锁就够；查询都很小，不需要异步驱动。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  goal TEXT NOT NULL,
  project TEXT,
  session TEXT,
  status TEXT NOT NULL DEFAULT 'pending',
  created_at REAL, started_at REAL, finished_at REAL,
  tokens_used INTEGER DEFAULT 0,
  step_limit INTEGER DEFAULT 30,
  deadline REAL,
  report TEXT,
  extra TEXT
);
CREATE TABLE IF NOT EXISTS steps (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER NOT NULL,
  idx INTEGER NOT NULL,
  desc TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  depends_on TEXT DEFAULT '[]',
  verify TEXT DEFAULT '{}',
  attempts INTEGER DEFAULT 0,
  result TEXT, error TEXT,
  started_at REAL, finished_at REAL,
  UNIQUE(task_id, idx)
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER, step_id INTEGER,
  ts REAL, kind TEXT, payload TEXT
);
CREATE TABLE IF NOT EXISTS checkpoints (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER, step_idx INTEGER, blob TEXT, created_at REAL
);
CREATE TABLE IF NOT EXISTS llm_calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER, step_id INTEGER, purpose TEXT,
  prompt_tokens INTEGER DEFAULT 0, completion_tokens INTEGER DEFAULT 0,
  latency_ms INTEGER DEFAULT 0, ts REAL
);
CREATE TABLE IF NOT EXISTS outbox (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER, ts REAL, text TEXT, file TEXT, sent INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS approvals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER, step_id INTEGER, cmd TEXT,
  status TEXT DEFAULT 'pending', ts REAL, decided_at REAL
);
CREATE INDEX IF NOT EXISTS idx_steps ON steps(task_id, idx);
CREATE INDEX IF NOT EXISTS idx_events ON events(task_id, id);
CREATE INDEX IF NOT EXISTS idx_calls ON llm_calls(task_id);
CREATE INDEX IF NOT EXISTS idx_outbox ON outbox(sent, id);
"""


class Store:
    def __init__(self, path: Path | str | None = None) -> None:
        config.ensure_dirs()
        self.path = Path(path or (config.DATA / "agentd.db"))
        self._lock = threading.RLock()
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    # ---------- 任务 ----------
    def create_task(self, goal: str, project: str = "", session: str = "",
                    step_limit: int = 30, deadline: float | None = None,
                    files: list[str] | None = None,
                    context: str = "") -> int:
        payload: dict = {}
        if files:
            payload["files"] = files
        if context:
            payload["context"] = context[:800]
        extra = json.dumps(payload, ensure_ascii=False) if payload else ""
        with self._lock, self.db:
            cur = self.db.execute(
                "INSERT INTO tasks(goal,project,session,status,created_at,step_limit,"
                "deadline,extra) VALUES(?,?,?,'pending',?,?,?,?)",
                (goal, project, session, time.time(), step_limit,
                 deadline or (time.time() + 3600), extra))
            return int(cur.lastrowid or 0)

    def task(self, task_id: int) -> dict | None:
        with self._lock:
            r = self.db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return dict(r) if r else None

    def tasks(self, limit: int = 10) -> list[dict]:
        with self._lock:
            rows = self.db.execute(
                "SELECT * FROM tasks ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def set_task(self, task_id: int, **kw: Any) -> None:
        if not kw:
            return
        cols = ",".join(f"{k}=?" for k in kw)
        with self._lock, self.db:
            self.db.execute(f"UPDATE tasks SET {cols} WHERE id=?", (*kw.values(), task_id))

    def next_pending_task(self) -> dict | None:
        """队列：FIFO，只取最早的一个 pending。"""
        with self._lock:
            r = self.db.execute(
                "SELECT * FROM tasks WHERE status='pending' ORDER BY id LIMIT 1").fetchone()
        return dict(r) if r else None

    def running_task(self) -> dict | None:
        with self._lock:
            r = self.db.execute(
                "SELECT * FROM tasks WHERE status='running' ORDER BY id LIMIT 1").fetchone()
        return dict(r) if r else None

    # ---------- 步骤 ----------
    def add_steps(self, task_id: int, steps: list[dict]) -> None:
        with self._lock, self.db:
            for s in steps:
                self.db.execute(
                    "INSERT OR REPLACE INTO steps(task_id,idx,desc,status,depends_on,verify)"
                    " VALUES(?,?,?,?,?,?)",
                    (task_id, int(s["idx"]), s["desc"], s.get("status", "pending"),
                     json.dumps(s.get("depends_on") or [], ensure_ascii=False),
                     json.dumps(s.get("verify") or {}, ensure_ascii=False)))

    def steps(self, task_id: int) -> list[dict]:
        with self._lock:
            rows = self.db.execute(
                "SELECT * FROM steps WHERE task_id=? ORDER BY idx", (task_id,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["depends_on"] = json.loads(d.get("depends_on") or "[]")
            d["verify"] = json.loads(d.get("verify") or "{}")
            out.append(d)
        return out

    def step(self, step_id: int) -> dict | None:
        with self._lock:
            r = self.db.execute("SELECT * FROM steps WHERE id=?", (step_id,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["depends_on"] = json.loads(d.get("depends_on") or "[]")
        d["verify"] = json.loads(d.get("verify") or "{}")
        return d

    def set_step(self, step_id: int, **kw: Any) -> None:
        if not kw:
            return
        cols = ",".join(f"{k}=?" for k in kw)
        with self._lock, self.db:
            self.db.execute(f"UPDATE steps SET {cols} WHERE id=?", (*kw.values(), step_id))

    # ---------- 事件 ----------
    def add_event(self, task_id: int, kind: str, payload: Any,
                  step_id: int | None = None) -> None:
        if not isinstance(payload, str):
            payload = json.dumps(payload, ensure_ascii=False)
        with self._lock, self.db:
            self.db.execute(
                "INSERT INTO events(task_id,step_id,ts,kind,payload) VALUES(?,?,?,?,?)",
                (task_id, step_id, time.time(), kind, payload[:4000]))

    def events(self, task_id: int, limit: int = 50, kinds: list[str] | None = None) -> list[dict]:
        sql = "SELECT * FROM events WHERE task_id=?"
        args: list[Any] = [task_id]
        if kinds:
            sql += f" AND kind IN ({','.join('?' * len(kinds))})"
            args += kinds
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self.db.execute(sql, args).fetchall()
        return [dict(r) for r in reversed(rows)]

    # ---------- 检查点 ----------
    def save_checkpoint(self, task_id: int, step_idx: int, blob: dict) -> None:
        with self._lock, self.db:
            self.db.execute(
                "INSERT INTO checkpoints(task_id,step_idx,blob,created_at) VALUES(?,?,?,?)",
                (task_id, step_idx, json.dumps(blob, ensure_ascii=False), time.time()))

    def last_checkpoint(self, task_id: int) -> dict | None:
        with self._lock:
            r = self.db.execute(
                "SELECT * FROM checkpoints WHERE task_id=? ORDER BY id DESC LIMIT 1",
                (task_id,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["blob"] = json.loads(d["blob"])
        return d

    # ---------- LLM 记账 ----------
    def record_call(self, task_id: int | None, purpose: str,
                    prompt_tokens: int, completion_tokens: int,
                    latency_ms: int, step_id: int | None = None) -> None:
        with self._lock, self.db:
            self.db.execute(
                "INSERT INTO llm_calls(task_id,step_id,purpose,prompt_tokens,"
                "completion_tokens,latency_ms,ts) VALUES(?,?,?,?,?,?,?)",
                (task_id, step_id, purpose, prompt_tokens, completion_tokens,
                 latency_ms, time.time()))
            if task_id:
                self.db.execute(
                    "UPDATE tasks SET tokens_used=tokens_used+? WHERE id=?",
                    (prompt_tokens + completion_tokens, task_id))

    def tokens_today(self) -> int:
        cut = time.time() - 86400
        with self._lock:
            r = self.db.execute(
                "SELECT COALESCE(SUM(prompt_tokens+completion_tokens),0) FROM llm_calls"
                " WHERE ts>?", (cut,)).fetchone()
        return int(r[0] or 0)

    # ---------- 发件箱（给 QQ 插件拉取）----------
    def outbox_push(self, task_id: int | None, text: str, file: str = "") -> None:
        with self._lock, self.db:
            self.db.execute(
                "INSERT INTO outbox(task_id,ts,text,file,sent) VALUES(?,?,?,?,0)",
                (task_id, time.time(), text, file))

    def outbox_since(self, since_id: int = 0, limit: int = 20) -> list[dict]:
        with self._lock:
            rows = self.db.execute(
                "SELECT * FROM outbox WHERE id>? ORDER BY id LIMIT ?",
                (since_id, limit)).fetchall()
        return [dict(r) for r in rows]

    def outbox_mark(self, up_to_id: int) -> None:
        with self._lock, self.db:
            self.db.execute("UPDATE outbox SET sent=1 WHERE id<=?", (up_to_id,))

    def outbox_head(self) -> int:
        with self._lock:
            r = self.db.execute("SELECT COALESCE(MAX(id),0) FROM outbox").fetchone()
        return int(r[0] or 0)

    def pending_count(self) -> int:
        with self._lock:
            r = self.db.execute(
                "SELECT COUNT(*) FROM tasks WHERE status IN ('pending','running')"
            ).fetchone()
        return int(r[0] or 0)

    def replanned(self, task_id: int) -> bool:
        t = self.task(task_id) or {}
        try:
            return bool(json.loads(t.get("extra") or "{}").get("replanned"))
        except Exception:  # noqa: BLE001
            return False

    def mark_replanned(self, task_id: int) -> None:
        t = self.task(task_id) or {}
        try:
            extra = json.loads(t.get("extra") or "{}")
        except Exception:  # noqa: BLE001
            extra = {}
        extra["replanned"] = True
        self.set_task(task_id, extra=json.dumps(extra, ensure_ascii=False))

    def answer(self, task_id: int, text: str) -> int:
        """回答一个 needs_input 的任务。

        语义：**卡住的步骤作废**（它们已经证明走不通），并允许再规划一次。
        重新跑同样的步骤只会再失败一遍——用户给的是「换条路」的信息。
        """
        self.add_event(task_id, "answer", text[:1000])
        n = 0
        for s in self.steps(task_id):
            if s["status"] in ("blocked", "skipped"):
                self.set_step(s["id"], status="skipped", error="用户已答复，改走新方案")
                n += 1
        self.clear_replanned(task_id)          # 给一次新的重规划机会
        self.set_task(task_id, status="pending", finished_at=None)
        return n

    def clear_replanned(self, task_id: int) -> None:
        t = self.task(task_id) or {}
        try:
            extra = json.loads(t.get("extra") or "{}")
        except Exception:  # noqa: BLE001
            extra = {}
        extra.pop("replanned", None)
        self.set_task(task_id, extra=json.dumps(extra, ensure_ascii=False))

    def expire_waiting(self, hours: int = 24) -> int:
        """needs_input 挂太久就当失败，别让任务永久停在等人回答。"""
        cut = time.time() - hours * 3600
        with self._lock, self.db:
            cur = self.db.execute(
                "UPDATE tasks SET status='failed', report='等你回答/审批超时，已关闭'"
                " WHERE status='needs_input' AND COALESCE(finished_at,0) < ?", (cut,))
            return int(cur.rowcount or 0)

    def outbox_trim(self, keep_days: int = 7, keep_rows: int = 200) -> int:
        """清掉旧的已发消息：避免无限增长，也让重启时没有可重放的历史。"""
        cut = time.time() - keep_days * 86400
        with self._lock, self.db:
            cur = self.db.execute("DELETE FROM outbox WHERE sent=1 AND ts<?", (cut,))
            n = int(cur.rowcount or 0)
            cur = self.db.execute(
                "DELETE FROM outbox WHERE sent=1 AND id NOT IN"
                " (SELECT id FROM outbox ORDER BY id DESC LIMIT ?)", (keep_rows,))
            return n + int(cur.rowcount or 0)

    # ---------- 审批（危险命令）----------
    def add_approval(self, task_id: int, step_id: int, cmd: str) -> int:
        with self._lock, self.db:
            cur = self.db.execute(
                "INSERT INTO approvals(task_id,step_id,cmd,status,ts)"
                " VALUES(?,?,?,'pending',?)", (task_id, step_id, cmd[:500], time.time()))
            return int(cur.lastrowid or 0)

    def approvals(self, status: str = "pending", limit: int = 10) -> list[dict]:
        with self._lock:
            rows = self.db.execute(
                "SELECT * FROM approvals WHERE status=? ORDER BY id DESC LIMIT ?",
                (status, limit)).fetchall()
        return [dict(r) for r in rows]

    def set_approval(self, aid: int, status: str) -> None:
        with self._lock, self.db:
            self.db.execute("UPDATE approvals SET status=?, decided_at=? WHERE id=?",
                            (status, time.time(), aid))

    def approved_cmd(self, task_id: int, step_id: int, cmd: str) -> bool:
        """这一步的这条命令是否被批准过（用过一次就作废）。"""
        with self._lock:
            r = self.db.execute(
                "SELECT id FROM approvals WHERE task_id=? AND step_id=? AND cmd=?"
                " AND status='approved' ORDER BY id DESC LIMIT 1",
                (task_id, step_id, cmd[:500])).fetchone()
        if not r:
            return False
        self.set_approval(int(r["id"]), "used")
        return True

    # ---------- 用量 ----------
    def call_breakdown(self) -> dict:
        cut = time.time() - 86400
        with self._lock:
            rows = self.db.execute(
                "SELECT purpose, COUNT(*) n, SUM(prompt_tokens+completion_tokens) t"
                " FROM llm_calls WHERE ts>? GROUP BY purpose", (cut,)).fetchall()
        return {r["purpose"]: {"calls": int(r["n"]), "tokens": int(r["t"] or 0)}
                for r in rows}

    # ---------- 工作区清理 ----------
    def cleanup_workspace(self, work: Path, keep_tasks: int = 20,
                          log_days: int = 7) -> dict:
        """删旧日志、归档旧任务的产物。PLAN.md/report 永不删。"""
        import shutil

        removed_logs = 0
        archived = 0
        cut = time.time() - log_days * 86400
        if not work.exists():
            return {"logs": 0, "archived": 0}
        for d in sorted(work.iterdir()):
            if not d.is_dir():
                continue
            logs = d / ".logs"
            if logs.exists():
                for f in logs.iterdir():
                    try:
                        if f.is_file() and f.stat().st_mtime < cut:
                            f.unlink()
                            removed_logs += 1
                    except OSError:
                        pass
        done = [t for t in self.tasks(500) if t["status"] in ("done", "failed", "cancelled")]
        for t in done[keep_tasks:]:
            d = work / str(t["id"])
            if not d.exists():
                continue
            keep = {"PLAN.md", ".logs"}
            big = [p for p in d.iterdir() if p.name not in keep]
            if not big:
                continue
            arc = work / "archive"
            arc.mkdir(exist_ok=True)
            target = arc / f"task-{t['id']}"
            target.mkdir(exist_ok=True)
            for p in big:
                try:
                    shutil.move(str(p), str(target / p.name))
                    archived += 1
                except OSError:
                    pass
        return {"logs": removed_logs, "archived": archived}
