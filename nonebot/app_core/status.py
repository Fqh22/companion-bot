"""一眼看记忆链路是否健康。

为什么需要：记忆是「总结 → 事实 → 向量 → 召回 → 矛盾 → 画像」一长串步骤，
任何一步静默失败（embedding 抖一下、总结连续失败），表现都只是「它变傻了」。
没有一个总览，排障只能靠猜是哪一环。

这里只读不写：数几个数、取几个时间戳。所有查询都容错——全新库上也要能跑。
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from app_core import paths as _paths

# 影子日志 = 调参用的样本，体积能反映「这一周跑了多少轮」
_SHADOW_GLOBS = ("shadow-*.jsonl", "contradictions.jsonl")


def _ro(db: Path) -> sqlite3.Connection | None:
    try:
        if not db.exists():
            return None
        return sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
    except Exception:  # noqa: BLE001
        return None


def _mem(c: sqlite3.Connection | None) -> dict:
    out = {"total": 0, "active": 0, "archived": 0, "superseded": 0,
           "last_access": 0.0, "last_write": 0.0}
    if c is None:
        return out
    try:
        for status, n in c.execute(
                "SELECT COALESCE(json_extract(metadata,'$.status'),'active') s, "
                "COUNT(*) FROM documents GROUP BY s"):
            out["total"] += n
            if status in out:
                out[status] = n
        out["last_access"] = float(c.execute(
            "SELECT COALESCE(MAX(CAST(json_extract(metadata,'$.last_access_time') "
            "AS REAL)), 0) FROM documents").fetchone()[0] or 0)
        out["last_write"] = float(c.execute(
            "SELECT COALESCE(MAX(CAST(json_extract(metadata,'$.create_time') "
            "AS REAL)), 0) FROM documents").fetchone()[0] or 0)
    except Exception:  # noqa: BLE001
        pass
    return out


def _chat(c: sqlite3.Connection | None) -> dict:
    out = {"messages": 0, "last_message": 0.0, "sessions_summarized": 0,
           "wiki_pages": 0}
    if c is None:
        return out
    for key, sql in (
        ("messages", "SELECT COUNT(*) FROM messages"),
        ("last_message", "SELECT COALESCE(MAX(ts),0) FROM messages"),
        ("sessions_summarized", "SELECT COUNT(*) FROM summaries"),
        ("wiki_pages", "SELECT COUNT(*) FROM wiki_pages"),
    ):
        try:
            v = c.execute(sql).fetchone()[0]
            out[key] = float(v or 0) if key == "last_message" else int(v or 0)
        except Exception:  # noqa: BLE001
            pass
    return out


def _contradictions(data_dir: Path, since: float) -> int:
    """窗口内做了多少次矛盾判定（不是改了多少条，是「问过多少次模型」）。"""
    n = 0
    try:
        p = data_dir / "contradictions.jsonl"
        if not p.exists():
            return 0
        with p.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    if float(json.loads(line).get("ts") or 0) >= since:
                        n += 1
                except Exception:  # noqa: BLE001
                    continue
    except Exception:  # noqa: BLE001
        return 0
    return n


def _shadow_bytes(data_dir: Path) -> int:
    total = 0
    for pat in _SHADOW_GLOBS:
        for p in data_dir.glob(pat):
            try:
                total += p.stat().st_size
            except Exception:  # noqa: BLE001
                pass
    return total


def snapshot(now: float | None = None, data_dir: Path | None = None,
             mem_db: Path | None = None, chat_db: Path | None = None) -> dict:
    """返回一份可渲染的健康快照。所有路径可注入，方便测试。"""
    now = time.time() if now is None else now
    data = Path(data_dir) if data_dir else _paths.DATA
    mc = _ro(Path(mem_db) if mem_db else _paths.MEM_DB)
    cc = _ro(Path(chat_db) if chat_db else _paths.CHAT_DB)
    try:
        mem = _mem(mc)
        chat = _chat(cc)
    finally:
        for c in (mc, cc):
            if c is not None:
                c.close()
    return {
        "now": now,
        "mem": mem,
        "chat": chat,
        "today": {"contradictions": _contradictions(data, now - 86400)},
        "files": {"shadow_bytes": _shadow_bytes(data)},
    }


def _ago(ts: float, now: float) -> str:
    if not ts:
        return "从未"
    d = max(0.0, now - ts)
    if d < 60:
        return "刚刚"
    if d < 3600:
        return f"{int(d // 60)} 分钟前"
    if d < 86400:
        return f"{d / 3600:.0f} 小时前"
    return f"{d / 86400:.0f} 天前"


def render(s: dict) -> str:
    """几行字。/mem_status 用它，周报也能复用。"""
    m, c = s["mem"], s["chat"]
    now = s["now"]
    lines = [
        f"记忆 {m['total']} 条（活跃 {m['active']} / 归档 {m['archived']} / "
        f"作废 {m['superseded']}）",
        f"最近召回 {_ago(m['last_access'], now)}｜最近写入 {_ago(m['last_write'], now)}",
        f"消息 {c['messages']} 条｜纪要 {c['sessions_summarized']} 个会话｜"
        f"知识 {c['wiki_pages']} 页",
        f"最近一条消息 {_ago(c['last_message'], now)}｜"
        f"今日矛盾判定 {s['today']['contradictions']} 次",
        f"影子日志 {s['files']['shadow_bytes'] // 1024} KB",
    ]
    return chr(10).join(lines)
