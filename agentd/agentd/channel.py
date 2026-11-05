"""HTTP 接口：给 QQ 插件（或任何前端）调用。只依赖标准库，跑在后台线程。

契约很薄：创建任务 / 查状态 / 暂停恢复取消 / 拉取待发消息。
真正的执行在 asyncio 主循环里，这里只改数据库状态。
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .config import Config
from .store import Store


class _Handler(BaseHTTPRequestHandler):
    store: Store
    cfg: Config

    def log_message(self, *a):  # 静音
        pass

    def _page(self) -> None:
        """极简看板：只读，5 秒自刷。只监听回环，靠 SSH 隧道访问。"""
        rows = []
        for t in self.store.tasks(20):
            color = {"done": "#2e7d32", "running": "#1565c0", "failed": "#c62828",
                     "needs_input": "#ef6c00", "paused": "#6a1b9a"}.get(
                         t["status"], "#444")
            rows.append(
                f"<tr><td>#{t['id']}</td>"
                f"<td style='color:{color};font-weight:600'>{t['status']}</td>"
                f"<td>{_esc(t['goal'][:70])}</td>"
                f"<td>{t['tokens_used']}</td>"
                f"<td>{_esc((t.get('report') or '')[:160])}</td></tr>")
        html = f"""<!doctype html><meta charset="utf-8">
<meta http-equiv="refresh" content="5">
<title>agentd</title>
<style>body{{font:14px/1.5 system-ui;margin:24px}}
table{{border-collapse:collapse;width:100%}}
td,th{{border-bottom:1px solid #eee;padding:6px;text-align:left;vertical-align:top}}
th{{background:#fafafa}}</style>
<h2>agentd <small style="color:#888">今日 {self.store.tokens_today()} tokens</small></h2>
<table><tr><th>#</th><th>状态</th><th>目标</th><th>tokens</th><th>报告</th></tr>
{''.join(rows) or '<tr><td colspan=5>还没有任务</td></tr>'}</table>
<p style="color:#888">每 5 秒刷新。派活：QQ 里 <code>/task 目标</code></p>"""
        body = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ---------- 工具 ----------
    def _json(self, obj, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:  # noqa: BLE001
            return {}

    # ---------- 路由 ----------
    def do_GET(self) -> None:  # noqa: N802
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/":
            return self._page()
        if u.path == "/health":
            return self._json({"ok": True, "tokens_today": self.store.tokens_today()})
        if u.path == "/tasks":
            n = int((q.get("limit") or ["10"])[0])
            rows = [
                {"id": t["id"], "goal": t["goal"][:60], "status": t["status"],
                 "tokens": t["tokens_used"], "report": (t.get("report") or "")[:200]}
                for t in self.store.tasks(n)]
            return self._json({"ok": True, "tasks": rows})
        if u.path == "/outbox/head":
            return self._json({"ok": True, "max": self.store.outbox_head()})
        if u.path == "/usage":
            return self._json({"ok": True, "tokens_today": self.store.tokens_today(),
                               "by_purpose": self.store.call_breakdown()})
        if u.path == "/approvals":
            return self._json({"ok": True, "approvals": self.store.approvals()})
        if u.path == "/outbox":
            since = int((q.get("since") or ["0"])[0])
            msgs = self.store.outbox_since(since)
            return self._json({"ok": True, "messages": msgs})
        return self._json({"ok": False, "error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        u = urlparse(self.path)
        parts = [p for p in u.path.split("/") if p]
        body = self._body()

        if u.path == "/task":
            goal = str(body.get("goal") or "").strip()
            if not goal:
                return self._json({"ok": False, "error": "goal 为空"}, 400)
            if self.store.pending_count() >= 3:
                return self._json(
                    {"ok": False, "error": "队列满了（已有 3 个任务在排），先 /cancel 一个"}, 429)
            tid = self.store.create_task(
                goal, project=str(body.get("project") or ""),
                session=str(body.get("session") or ""),
                step_limit=self.cfg.max_steps,
                files=[str(x) for x in (body.get("files") or [])][:5],
                context=str(body.get("context") or ""))
            waiting = len([t for t in self.store.tasks(5) if t["status"] == "running"])
            return self._json({"ok": True, "id": tid, "waiting": waiting})

        if u.path == "/outbox/ack":
            self.store.outbox_mark(int(body.get("up_to") or 0))
            return self._json({"ok": True})

        if len(parts) == 2 and parts[0] == "approve":
            aid = int(parts[1])
            rows = [a for a in self.store.approvals("pending", 50) if a["id"] == aid]
            if not rows:
                return self._json({"ok": False, "error": "没有这条待审批"}, 404)
            a = rows[0]
            self.store.set_approval(aid, "approved")
            # 步骤和任务都回到可执行状态，主循环会重新捡起来
            self.store.set_step(a["step_id"], status="pending", error="")
            self.store.set_task(a["task_id"], status="pending", finished_at=None)
            return self._json({"ok": True, "id": aid,
                               "cmd": a["cmd"][:80], "task": a["task_id"]})

        if len(parts) == 3 and parts[0] == "task" and parts[2] == "answer":
            tid = int(parts[1])
            if not self.store.task(tid):
                return self._json({"ok": False, "error": "任务不存在"}, 404)
            text = str(body.get("text") or "").strip()
            if not text:
                return self._json({"ok": False, "error": "答复为空"}, 400)
            n = self.store.answer(tid, text)
            return self._json({"ok": True, "id": tid, "reopened": n})

        if len(parts) == 3 and parts[0] == "task":
            tid, action = int(parts[1]), parts[2]
            if not self.store.task(tid):
                return self._json({"ok": False, "error": "任务不存在"}, 404)
            if action == "cancel":
                self.store.set_task(tid, status="cancelled")
            elif action == "pause":
                self.store.set_task(tid, status="paused")
            elif action == "resume":
                self.store.set_task(tid, status="pending")
            else:
                return self._json({"ok": False, "error": "未知动作"}, 400)
            return self._json({"ok": True, "id": tid, "action": action})

        return self._json({"ok": False, "error": "not found"}, 404)


def _esc(s: str) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def start(store: Store, cfg: Config) -> threading.Thread:
    handler = type("H", (_Handler,), {"store": store, "cfg": cfg})
    srv = ThreadingHTTPServer(("127.0.0.1", cfg.port), handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return t
