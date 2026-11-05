"""表情包：优先按语义匹配（caption + tags 向量），回退到分类。"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import sqlite3
import time
from pathlib import Path

from app_core import paths as _paths

from app_core.memory import Memory
from app_core.vec import cos, pack, unpack

log = logging.getLogger("qqbot.meme")

PACKS = _paths.QDATA / "meme_packs"
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

# 最近发过的图（落盘、带时间戳）。原来放在内存里，重启就忘——
# 而「重启」包括发版、systemd 拉起、崩了自愈，一天可能好几次。
RECENT_FILE = _paths.DATA / "meme-recent.json"
RECENT_WINDOW_H = 24
RECENT_KEEP = 80

# 本次进程内的匹配方式计数：语义命中率是判断「向量这一层有没有真的在工作」
# 的唯一指标。重启归零，看趋势用 /meme_stats 的当前值即可。
_hits: dict[str, int] = {"semantic": 0, "category": 0, "none": 0}


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
    # 扫**所有包**下的 *.json，而不是写死某一个包：新加一个包时图片扫得到、
    # 却永远没有向量，只会静默落进分类回退，而且查不出为什么。
    try:
        for f in sorted(PACKS.rglob("*.json")):
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
            except Exception as e:  # noqa: BLE001
                log.warning("表情包元数据解析失败 %s：%s", f.name, e)
                continue
            imgs = d.get("images")
            if not isinstance(imgs, dict):
                continue
            base = f.parent
            for v in imgs.values():
                if not isinstance(v, dict):
                    continue
                rel = v.get("relative_path")
                if not rel:
                    continue
                out[str(base / rel)] = {
                    "caption": v.get("caption") or "",
                    "tags": v.get("tags") or [],
                    "category": v.get("category") or "",
                }
    except Exception as e:  # noqa: BLE001
        log.warning("表情包目录扫描失败：%s", e)
    _meta = out
    return out


def stats() -> dict:
    items = scan()
    cats: dict[str, int] = {}
    for c, _ in items:
        cats[c] = cats.get(c, 0) + 1
    try:
        c = _conn()
        n = c.execute("SELECT COUNT(*) FROM meme_vectors").fetchone()[0]
        c.close()
    except Exception:  # noqa: BLE001
        n = 0
    return {"total": len(items), "categories": cats, "described": len(meta()),
            "vectorized": int(n), "recent_24h": len(_recent_paths()),
            "hits": dict(_hits)}


def _recent_load() -> list[dict]:
    try:
        rows = json.loads(RECENT_FILE.read_text(encoding="utf-8"))
        return [r for r in rows if isinstance(r, dict) and r.get("path")]
    except Exception:  # noqa: BLE001
        return []


def _recent_paths(now: float | None = None) -> set[str]:
    """24 小时内发过的。落盘，重启不丢。"""
    now = time.time() if now is None else now
    cutoff = now - RECENT_WINDOW_H * 3600
    return {str(r["path"]) for r in _recent_load()
            if float(r.get("ts") or 0) >= cutoff}


def _remember(path: str, now: float | None = None) -> None:
    now = time.time() if now is None else now
    cutoff = now - RECENT_WINDOW_H * 3600
    rows = [r for r in _recent_load() if float(r.get("ts") or 0) >= cutoff]
    rows.append({"path": path, "ts": now})
    try:
        RECENT_FILE.parent.mkdir(parents=True, exist_ok=True)
        RECENT_FILE.write_text(json.dumps(rows[-RECENT_KEEP:], ensure_ascii=False),
                               encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        log.debug("表情包去重记录写入失败：%s", e)


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
                              (path, pack(v), time.time()))
                    done += 1
        c.close()
    return done


def _pick(cands: list[str]) -> str:
    """从候选里挑一张，优先避开最近发过的。

    没有记录会导致同一张图一天里被发好几次，一眼就露馅。
    """
    if not cands:
        return ""
    recent = _recent_paths()
    pool = [p for p in cands if p not in recent] or cands
    choice = random.choice(pool)
    _remember(choice)
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


# 发送前压缩：包里混着 800KB+ 的 PNG，base64 之后 1.2MB，走 WS 太重。
# 表情包是贴纸，长边 800、JPEG 85 足够；gif/webp 不动（动画会丢，且本来就小）。
MAX_SEND_BYTES = 300 * 1024
MAX_SEND_SIDE = 800


def payload(path: str) -> bytes:
    """读成「适合直接发」的 bytes。压缩失败就退回原图，不影响发送。"""
    raw = Path(path).read_bytes()
    if len(raw) <= MAX_SEND_BYTES or Path(path).suffix.lower() not in (".png", ".jpg", ".jpeg"):
        return raw
    try:
        import io

        from PIL import Image

        im = Image.open(io.BytesIO(raw)).convert("RGB")
        im.thumbnail((MAX_SEND_SIDE, MAX_SEND_SIDE))
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=85)
        out = buf.getvalue()
        if len(out) < len(raw):
            return out
    except Exception as e:  # noqa: BLE001
        log.warning("表情包压缩失败，按原图发送：%s", e)
    return raw


def _vectors() -> list[tuple[str, bytes]]:
    """同步读向量。调用方用 asyncio.to_thread 包住，别阻塞事件循环。"""
    init()
    c = _conn()
    try:
        return c.execute("SELECT path, vec FROM meme_vectors").fetchall()
    finally:
        c.close()


async def find_ex(query: str) -> tuple[str, str, float]:
    """返回 (路径, 匹配方式, 分数)。方式：semantic / category / none。

    为什么要把方式返回出去：语义命中 top-1 和掉进随机分类是两件事，
    模型需要知道这张到底贴不贴，才能决定要不要补一句。
    """
    q = (query or "").strip()
    if q:
        try:
            rows = await asyncio.to_thread(_vectors)
            if rows:
                mem = Memory()
                qv = (await mem.embed([q]))[0]
                if qv:
                    scored = [(p, cos(qv, unpack(v))) for p, v in rows]
                    scored.sort(key=lambda x: -x[1])
                    top = [p for p, s in scored[:8] if s >= 0.30]
                    if top:
                        _hits["semantic"] += 1
                        return _pick(top), "semantic", round(scored[0][1], 3)
            else:
                log.warning("表情包向量库为空，落回分类匹配")
        except Exception as e:  # noqa: BLE001
            log.warning("表情包语义匹配失败，回退分类：%s", e)
    path = _category_fallback(q)
    mode = "category" if path else "none"
    _hits[mode] = _hits.get(mode, 0) + 1
    return path, mode, 0.0


async def find(query: str) -> str:
    """兼容旧调用：只要路径。"""
    path, _, _ = await find_ex(query)
    return path


def describe(path: str) -> str:
    d = meta().get(path) or {}
    return (d.get("caption") or "")[:120]


def categories() -> dict:
    return dict(CAT_KEYS)
