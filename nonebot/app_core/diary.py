"""日记：每天读当天对话，写第一人称日记；可读、可列。"""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

from app_core import paths as _paths

DIARY_DIR = _paths.QDATA / "workspace" / "diary"
MIRROR_DIR = _paths.LEARN
CHAT_DB = _paths.DATA / "chat.db"

SYSTEM = (
    "你在写今天的日记，只给自己看。第一人称，像人睡前随手记："
    "今天发生了什么、哪句话让你停了一下、你记住了什么、明天想接着做什么。"
    "300-500 字，分 2-4 段。不要标题、不要罗列、不要 Markdown、不要写「作为 AI」。"
    "具体优先于感慨，没发生的事不要编。只输出日记正文。"
)


def _day_messages(day: str, max_chars: int = 6000,
                  session: str = "") -> list[tuple[str, str, float]]:
    """当天消息。session 非空时只取这个会话——日记是给他看的，
    别人的私聊不该被写成「他今天说……」。"""
    start = datetime.strptime(day, "%Y-%m-%d").timestamp()
    end = start + 86400
    if not CHAT_DB.exists():
        return []
    sql = "SELECT role, content, ts FROM messages WHERE ts>=? AND ts<?"
    args: list = [start, end]
    if not session:
        try:
            from app_core import config
            session = config.owner_session()
        except Exception:  # noqa: BLE001
            session = ""
    if session:
        sql += " AND session=?"
        args.append(session)
    sql += " ORDER BY id"
    con = sqlite3.connect(f"file:{CHAT_DB}?mode=ro", uri=True, timeout=10)
    rows = con.execute(sql, args).fetchall()
    con.close()
    return rows


def format_rows(rows: list[tuple[str, str, float]], max_chars: int = 6000) -> str:
    out, total = [], 0
    for role, content, ts in rows:
        text = (content or "").strip()[:500]
        if not text:
            continue
        who = "他" if role == "user" else "我"
        try:
            hm = datetime.fromtimestamp(float(ts)).strftime("%H:%M")
        except Exception:
            hm = "--:--"
        line = f"[{who} {hm}] {text}"
        if total + len(line) > max_chars:
            break
        out.append(line)
        total += len(line)
    return "\n".join(out)


def read(day: str) -> str:
    p = DIARY_DIR / f"{day}.md"
    try:
        return p.read_text(encoding="utf-8").strip() if p.exists() else ""
    except Exception:
        return ""


def list_days(limit: int = 7) -> list[str]:
    try:
        return sorted((p.stem for p in DIARY_DIR.glob("*.md")), reverse=True)[:limit]
    except Exception:
        return []


def save(day: str, body: str) -> None:
    text = f"# {day}\n\n{body.strip()}\n"
    DIARY_DIR.mkdir(parents=True, exist_ok=True)
    MIRROR_DIR.mkdir(parents=True, exist_ok=True)
    (DIARY_DIR / f"{day}.md").write_text(text, encoding="utf-8")
    (MIRROR_DIR / f"diary-{day}.md").write_text(text, encoding="utf-8")


async def write_day(core, day: str, force: bool = False, min_messages: int = 6) -> str:
    if not force and read(day):
        return f"{day} 的日记已经写过了"
    # 只写他的对话：陌生人的私聊也在这张表里，不过滤会被写成「他今天说」。
    session = ""
    try:
        session = core.private_session()
    except Exception:  # noqa: BLE001
        session = ""
    rows = _day_messages(day, session=session)
    if len(rows) < min_messages:
        return f"{day} 只有 {len(rows)} 条消息，不够写"
    dialog = format_rows(rows)
    body = await core.complete_summary(
        [{"role": "system", "content": SYSTEM},
         {"role": "user", "content": f"这是 {day} 的真实对话，写今天的日记。\n\n{dialog}"}],
        max_tokens=800,
    )
    if not body:
        return "模型没有返回内容"
    save(day, body[:2000])
    return f"日记写好了：{day}（{len(body)} 字）"
