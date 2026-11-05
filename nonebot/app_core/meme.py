"""表情包：优先按语义匹配（caption + tags 向量），回退到分类。"""
from __future__ import annotations

import json
import random
import sqlite3
import time
from pathlib import Path

from app_core import paths as _paths

from app_core.memory import Memory, _cos, _pack, _unpack

PACKS = _paths.QDATA / "meme_packs"
META = PACKS / "deepseek-fanart-meme-pack" / "semantic_metadata.json"
CHAT_DB = _paths.DATA / "chat.db"
EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}

CAT_KEYS = {
    "tech": ["技术", "代码", "编程", "程序", "debug", "bug", "部署", "服务器"],
    "teasing": ["调侃", "吐槽", "损", "嘲讽", "阴阳", "玩笑"],
    "affection": ["喜欢", "亲昵", "抱", "贴贴", "可爱", "爱"],
    "work": ["工作", "上班", "加班", "打工", "开会", "项目"],
    "happy": ["开心", "高兴", "笑", "哈哈", "好耶", "乐"],
    "portrait": ["肖像", "自拍", "本人", "照"],
    "angry": ["生气", "气", "怒", "烦", "暴躁"],
    "greeting": ["你好", "打招呼", "早", "晚安", "在吗"],
    "confused": ["疑惑", "不懂", "问号", "啥", "懵"],
    "sad": ["难过", "伤心", "哭", "委屈", "emo"],
    "food": ["吃", "饿", "饭", "夜宵", "外卖"],
    "money": ["钱", "穷", "工资", "花钱", "贵"],
    "sleep": ["困", "睡", "熬夜", "床", "累"],
}

_cache: list[tuple[str, Path]] | None = None
_meta: dict[str, dict] | None = None
# 最近发过的几张，避免同一天反复发同一张
_recent: list[str] = []
RECENT_LIMIT = 5


def scan() -> list[tuple[str, Path]]:
    global _cache
    if _cache is not None:
        return _cache
    out: list[tuple[str, Path]] = []
    if PACKS.exists():
        for p in PACKS.rglob("*"):
            if p.is_file() and p.suffix.lower() in EXTS:
                out.append((p.parent.name, p))
    _cache = out
    return out


def meta() -> dict[str, dict]:
    """path -> {caption, tags, category}"""
    global _meta
    if _meta is not None:
        return _meta
    out: dict[str, dict] = {}
    try:
        d = json.loads(META.read_text(encoding="utf-8"))
        base = META.parent
        for _, v in (d.get("images") or {}).items():
            rel = v.get("relative_path")
            if not rel:
                continue
            out[str(base / rel)] = {
                "caption": v.get("caption") or "",
                "tags": v.get("tags") or [],
                "category": v.get("category") or "",
            }
    except Exception:
        pass
    _meta = out
    return out


def stats() -> dict:
    items = scan()
    cats: dict[str, int] = {}
    for c, _ in items:
        cats[c] = cats.get(c, 0) + 1
    return {"total": len(items), "categories": cats, "described": len(meta())}


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(CHAT_DB, timeout=15)
    c.execute("PRAGMA journal_mode=WAL")
    return c


def init() -> None:
    c = _conn()
    with c:
        c.execute("CREATE TABLE IF NOT EXISTS meme_vectors (path TEXT PRIMARY KEY, vec BLOB NOT NULL, ts REAL NOT NULL)")
    c.close()


async def ensure_vectors(batch: int = 16) -> int:
    init()
    m = meta()
    if not m:
        return 0
    c = _conn()
    have = {r[0] for r in c.execute("SELECT path FROM meme_vectors").fetchall()}
    c.close()
    todo = [(p, d) for p, d in m.items() if p not in have]
    if not todo:
        return 0
    mem = Memory()
    done = 0
    for i in range(0, len(todo), batch):
        chunk = todo[i:i + batch]
        texts = [((d["caption"] or "") + " " + " ".join(d["tags"]))[:800] for _, d in chunk]
        vecs = await mem.embed(texts)
        c = _conn()
        with c:
            for (path, _), v in zip(chunk, vecs):
                if v:
                    c.execute("INSERT OR REPLACE INTO meme_vectors(path, vec, ts) VALUES (?,?,?)",
                              (path, _pack(v), time.time()))
                    done += 1
        c.close()
    return done


def _pick(cands: list[str]) -> str:
    """从候选里挑一张，优先避开最近发过的。

    没有记录会导致同一张图一天里被发好几次，一眼就露馅。
    """
    if not cands:
        return ""
    pool = [p for p in cands if p not in _recent] or cands
    choice = random.choice(pool)
    _recent.append(choice)
    del _recent[:-RECENT_LIMIT]
    return choice


def _category_fallback(query: str) -> str:
    items = scan()
    if not items:
        return ""
    q = (query or "").lower()
    cands: list[str] = []
    if q:
        for cat, p in items:
            if any(k in q for k in CAT_KEYS.get(cat, [])) or cat in q:
                cands.append(str(p))
        if not cands:
            cands = [str(p) for _, p in items if q in p.name.lower()]
    if not cands:
        cands = [str(p) for _, p in items]
    return _pick(cands)


async def find(query: str) -> str:
    """优先语义匹配 caption/tags，失败回退分类。"""
    q = (query or "").strip()
    if not q:
        return _category_fallback("")
    try:
        init()
        c = _conn()
        rows = c.execute("SELECT path, vec FROM meme_vectors").fetchall()
        c.close()
        if rows:
            mem = Memory()
            qv = (await mem.embed([q]))[0]
            if qv:
                scored = [(p, _cos(qv, _unpack(v))) for p, v in rows]
                scored.sort(key=lambda x: -x[1])
                top = [p for p, s in scored[:8] if s >= 0.30]
                if top:
                    return _pick(top)
    except Exception:
        pass
    return _category_fallback(q)


def describe(path: str) -> str:
    d = meta().get(path) or {}
    return (d.get("caption") or "")[:120]


def categories() -> dict:
    return dict(CAT_KEYS)
