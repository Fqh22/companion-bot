"""知识库：给 learn/*.md 切块建向量，按 query 检索。存在 chat.db，不碰记忆库。

为什么按「块」而不是按「文件」：

- 整篇文件建一个向量，等于把 3000 字压成一个点，长文档内部不同主题互相稀释
- 检索回来只取前 600 字，长文档后半部分永远读不到
- 每轮强制注入 3 篇全文，占了 prompt 的 19%，其中大半跟当前话题无关

现在：按 ## 标题切、单块 ≤400 字、块间重叠 80 字；检索 top-4 块、同文件最多 2 块、
阈值 0.42。项目维度用来收窄范围：消息里提到某个项目名时，只在该项目 +「通用」里找。
"""
from __future__ import annotations

import math
import os
import re
import sqlite3
import time
from pathlib import Path

from app_core import paths as _paths

import numpy as np

from app_core import learn as learn_mod
from app_core.memory import Memory
from app_core.vec import pack

LEARN = _paths.LEARN
DB = _paths.DATA / "chat.db"

# 学习笔记的时间衰减：半衰期比闲聊记忆长得多（知识不会因为放久了失效）
KB_TIME_TAU_DAYS = 120.0
KB_TIME_WEIGHT = 0.2
# 日志型文件（每日复盘/回忆）只索引这么多天内的，更早的留在磁盘但不参与检索
KB_INDEX_DAYS = 180
# 对话原文（learn/raw/）只索引最近两周：一天就几百行，全量索引会稀释召回
KB_RAW_DAYS = 14
# 单次 embedding 请求最多带多少块。原来是「8 个文件」一批，但一个文件可能切出上百块：
# 2026-11-02 起 learn/raw/2026-11-01.md 有 103 块，整批发过去智谱直接 400，
# 于是知识库索引**每小时失败一次、连续 29 小时**，新写的笔记全都搜不到。
EMBED_BATCH = 16

CHUNK_CHARS = 400        # 单块字数
CHUNK_OVERLAP = 80       # 块间重叠：一句话被切断时两边都还完整
TOP_CHUNKS = 3           # 一次最多注入几块
CHUNK_INJECT = 300       # 每块注入的字数上限
MIN_SCORE = 0.40         # 块短，余弦普遍比整文件高；实测 top1 多在 0.42-0.60
MAX_PER_FILE = 2         # 同一文件最多贡献几块，防止一个文件刷屏
DEFAULT_PROJECT = "通用"

_mem = Memory()
_cache: dict = {"sig": None, "mat": None, "meta": []}


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB, timeout=15)
    c.execute("PRAGMA journal_mode=WAL")
    return c


def init() -> None:
    c = _conn()
    with c:
        c.execute(
            "CREATE TABLE IF NOT EXISTS kb_chunks ("
            "path TEXT NOT NULL, idx INTEGER NOT NULL, project TEXT NOT NULL DEFAULT '通用',"
            "text TEXT NOT NULL, vec BLOB NOT NULL, mtime REAL NOT NULL, ts REAL NOT NULL,"
            "PRIMARY KEY(path, idx))")
        c.execute("CREATE INDEX IF NOT EXISTS idx_kb_project ON kb_chunks(project)")
    c.close()


# ---------- 切块 ----------
def _split_sections(text: str) -> list[str]:
    """按 ## 标题切；第一个 ## 之前的内容（标题/日期/项目）也单独算一块。"""
    parts = re.split(r"(?m)^(?=## )", text)
    return [p.strip() for p in parts if p.strip()]


def chunk_text(text: str, size: int = CHUNK_CHARS, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """切成 ≤size 字的块，块间重叠 overlap。太长的段硬切，但不切碎到无法理解。"""
    out: list[str] = []
    for sec in _split_sections(text):
        if len(sec) <= size:
            out.append(sec)
            continue
        start = 0
        while start < len(sec):
            out.append(sec[start:start + size])
            if start + size >= len(sec):
                break
            start += size - overlap
    return out


def parse_project(text: str) -> str:
    """项目名写在文件头 `- 项目：xxx`；没有就退回 `- 标签：`。"""
    return learn_mod.note_project(text)


def _files() -> list[Path]:
    """要建索引的 md。

    复盘 / 回忆这类「日志型」文件只索引最近 KB_INDEX_DAYS 天的：
    更早的仍留在磁盘上，只是不再参与检索。否则一年后知识库里
    堆着三百多份旧复盘，新写的笔记会被它们稀释掉。
    笔记和技能是知识本身，永久索引。

    `raw/` 更特殊：一天的对话原文就六百多行，切出几十块。全量索引会把
    「整理过的笔记」挤下去，所以只留最近 KB_RAW_DAYS 天——更早的照样在磁盘上，
    要追溯直接翻文件，不必占召回预算。
    """
    out = []
    if not LEARN.exists():
        return out
    now = time.time()
    cutoff = now - KB_INDEX_DAYS * 86400
    raw_cutoff = now - KB_RAW_DAYS * 86400
    for p in LEARN.rglob("*.md"):
        if p.name in ("self_profile.md", "style_notes.md"):
            continue
        if "notes" in p.parts or "skills" in p.parts:
            out.append(p)
            continue
        try:
            lim = raw_cutoff if "raw" in p.parts else cutoff
            if p.stat().st_mtime < lim:
                continue
        except Exception:
            continue
        out.append(p)
    return out


async def index(max_files: int = 200) -> int:
    """增量重建。文件没改就跳过；改了就把这个文件的所有块整体换掉。"""
    init()
    files = _files()
    current = {str(p) for p in files}
    c = _conn()
    old = {path: mtime for path, mtime in
           c.execute("SELECT path, MAX(mtime) FROM kb_chunks GROUP BY path")}
    stale = [k for k in old if k not in current]
    if stale:
        with c:
            c.executemany("DELETE FROM kb_chunks WHERE path=?", [(k,) for k in stale])
        for k in stale:
            old.pop(k, None)
    c.close()

    todo = []
    for p in files:
        key = str(p)
        try:
            mtime = p.stat().st_mtime
        except Exception:
            continue
        if key in old and old[key] >= mtime:
            continue
        try:
            text = p.read_text(encoding="utf-8").strip()
        except Exception:
            continue
        if text:
            todo.append((key, mtime, text))
    # 截断必须发生在「过滤掉没变的文件」之后：之前是先 files[:200] 再判 mtime，
    # 文件数超过 200 时，排在遍历序后面的新笔记永远进不了待办，也不报错。
    # 超预算时按改动时间从新到旧处理，新的先入库，旧的下一轮补。
    if len(todo) > max_files:
        todo.sort(key=lambda x: -x[1])
        todo = todo[:max_files]

    done = 0
    for i in range(0, len(todo), 8):          # 每批 8 个文件，控制单次 embedding 体积
        batch = todo[i:i + 8]
        owners: list[tuple] = []
        texts: list[str] = []
        for key, mtime, text in batch:
            project = parse_project(text)
            for idx, piece in enumerate(chunk_text(text)):
                owners.append((key, idx, project, mtime, piece))
                texts.append(piece)
        if not texts:
            continue
        vecs: list[list[float]] = []
        for j in range(0, len(texts), EMBED_BATCH):
            vecs.extend(await _mem.embed(texts[j:j + EMBED_BATCH]))
        c = _conn()
        with c:
            for key in {o[0] for o in owners}:
                c.execute("DELETE FROM kb_chunks WHERE path=?", (key,))
            for (key, idx, project, mtime, piece), v in zip(owners, vecs):
                if not v:
                    continue
                c.execute(
                    "INSERT OR REPLACE INTO kb_chunks"
                    "(path, idx, project, text, vec, mtime, ts) VALUES (?,?,?,?,?,?,?)",
                    (key, idx, project, piece, pack(v), mtime, time.time()))
                done += 1
        c.close()
    if done:
        _cache["sig"] = None          # 索引变了，矩阵缓存作废
    return done


# ---------- 检索 ----------
def _matrix():
    """全部块向量读成归一化矩阵，进程内缓存（与记忆召回同一套做法）。"""
    c = _conn()
    rows = c.execute(
        "SELECT path, idx, project, text, vec, ts FROM kb_chunks").fetchall()
    c.close()
    if not rows:
        _cache.update({"sig": None, "mat": None, "meta": []})
        return None, []
    sig = (len(rows), max(r[5] or 0 for r in rows))
    if _cache["mat"] is not None and _cache["sig"] == sig:
        return _cache["mat"], _cache["meta"]
    dim = len(rows[0][4]) // 4
    mat = np.frombuffer(b"".join(r[4] for r in rows), dtype=np.float32).reshape(len(rows), dim)
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    _cache.update({
        "sig": sig,
        "mat": mat / norms,
        "meta": [(r[0], r[1], r[2], r[3]) for r in rows],
    })
    return _cache["mat"], _cache["meta"]


def projects() -> list[str]:
    try:
        c = _conn()
        rows = c.execute("SELECT DISTINCT project FROM kb_chunks").fetchall()
        c.close()
        return [r[0] for r in rows if r[0] and r[0] != DEFAULT_PROJECT]
    except Exception:
        return []


def detect_project(query: str) -> str:
    """消息里点了某个项目名，就只在这个项目里找。多个命中取最长的那个。"""
    q = (query or "").lower()
    hits = [p for p in projects() if p.lower() in q]
    if not hits:
        return ""
    hits.sort(key=len, reverse=True)
    return hits[0]


def _is_curated(path: str) -> bool:
    """笔记和技能是「整理过的知识」，复盘/日记是对话流水。检索时优先前者。"""
    return "/notes/" in path or "/skills/" in path


async def search_meta(query: str, top_k: int = 4, qv: list[float] | None = None) -> list[dict]:
    """和 recall 同源，但带文件名——工具返回时标出处，模型才知道这段话是哪来的。

    先只在笔记/技能里找：复盘是对话流水，量又大，直接混在一起搜会把
    真正整理过的那几页挤掉（实测「agent skill 怎么缓存」top5 全是复盘）。
    """
    texts = await recall(query, top_k, qv=qv, curated=True)
    if not texts:
        texts = await recall(query, top_k, qv=qv)
    if not texts:
        return []
    c = _conn()
    rows = c.execute("SELECT path, text FROM kb_chunks").fetchall()
    c.close()
    out = []
    for t in texts:
        path = label = ""
        for p, full in rows:
            if (full or "").startswith(t[:40]):
                path = Path(p).stem
                label = learn_mod.page_label(p)
                break
        out.append({"path": path, "text": t, "label": label})
    return out


async def recall(query: str, top_k: int = TOP_CHUNKS,
                 min_score: float = MIN_SCORE,
                 qv: list[float] | None = None,
                 curated: bool | None = None) -> list[str]:
    """返回若干段笔记正文。qv 可由调用方预先算好传入，与记忆召回共用一次 embedding。"""
    init()
    mat, meta = _matrix()
    if mat is None:
        return []
    if qv is None:
        qv = (await _mem.embed([query]))[0]
    if not qv:
        return []
    if mat.shape[1] != len(qv):
        return []                    # 换过 embedding 模型：维度不一致就不硬算

    q = np.asarray(qv, dtype=np.float32)
    nq = float(np.linalg.norm(q))
    if nq == 0:
        return []
    cos = mat @ (q / nq)

    project = detect_project(query)
    now = time.time()
    cands = []
    for i, (path, _idx, proj, text) in enumerate(meta):
        if project and proj not in (project, DEFAULT_PROJECT):
            continue
        if curated is True and not _is_curated(path):
            continue
        if curated is False and _is_curated(path):
            continue
        s = float(cos[i])
        if s < min_score:
            continue
        try:
            age_days = max(0.0, (now - os.stat(path).st_mtime) / 86400.0)
        except Exception:
            age_days = 0.0
        # 学习笔记的衰减比闲聊记忆慢：知识不会因为放久了就失效。
        # 阈值仍按原始相似度卡，时间只调排序。
        rec = math.exp(-age_days / KB_TIME_TAU_DAYS)
        cands.append((s * (1.0 - KB_TIME_WEIGHT + KB_TIME_WEIGHT * rec), path, text))
    cands.sort(key=lambda x: -x[0])

    out, per_file, seen = [], {}, set()
    for _, path, text in cands:
        if len(out) >= top_k:
            break
        if per_file.get(path, 0) >= MAX_PER_FILE:
            continue
        head = (text or "")[:60]
        if head in seen:
            continue
        seen.add(head)
        per_file[path] = per_file.get(path, 0) + 1
        out.append((text or "")[:CHUNK_INJECT])
    return out
