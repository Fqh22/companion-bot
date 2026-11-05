"""记忆重建：拿一条旧记忆的原始对话，用**现在的**摘要 prompt 重跑一遍。

**为什么要它**：记忆是 append-only 的。摘要 prompt 改了（比如今天把 topics
改成复用词表），历史 119 条还是旧格式，只能等新数据慢慢稀释。没有这个工具，
每次调 prompt 都是「只对新数据生效」，改对改错都要等一周才知道。

**怎么找回原文**：`documents.metadata.source_range` 存着 `session:last_id-target`，
那就是当时总结的那段消息区间。回 chat.db 按区间捞出来重放。

**默认 dry-run**，只打印新旧对照；`--apply` 才写，写之前把旧行整条存进
`documents_history`，`--rollback <doc_id>` 可以退回去。

用法：
    python rebuild.py --limit 3              # 看 3 条会变成什么样
    python rebuild.py --doc <doc_id> --apply  # 只重建一条
    python rebuild.py --rollback <doc_id>     # 退回上一版
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, "/opt/nonebot")

MEM_DB = Path("/opt/qqbot-data/livingmemory.db")
CHAT_DB = Path("/opt/nonebot/data/chat.db")


def parse_range(meta: dict) -> tuple[str, int, int] | None:
    """`session:12-48` → (session, 12, 48)。格式不对就当没有。"""
    raw = str(meta.get("source_range") or "")
    if ":" not in raw or "-" not in raw:
        return None
    session, _, span = raw.rpartition(":")
    a, _, b = span.partition("-")
    try:
        return session, int(a), int(b)
    except ValueError:
        return None


def load_dialog(session: str, lo: int, hi: int) -> list[tuple[str, str]]:
    c = sqlite3.connect(f"file:{CHAT_DB}?mode=ro", uri=True)
    rows = c.execute(
        "SELECT role, content FROM messages WHERE session=? AND id>? AND id<=? "
        "ORDER BY id", (session, lo, hi)).fetchall()
    c.close()
    return [(r[0], r[1]) for r in rows]


async def rebuild_one(core, did: int, doc_id: str, text: str, meta: dict,
                      apply: bool) -> dict | None:
    from app_core import summarize as summarize_mod

    rng = parse_range(meta)
    if not rng:
        return {"doc_id": doc_id, "skip": "没有 source_range"}
    rows = load_dialog(*rng)
    if len(rows) < 4:
        return {"doc_id": doc_id, "skip": f"原文只有 {len(rows)} 条"}

    raw = await core.complete_summary(
        [{"role": "system", "content": summarize_mod.SYSTEM},
         {"role": "user", "content": (
             f"【旧纪要】\n{meta.get('digest') or '（没有）'}\n\n"
             f"【这段对话】\n\n{summarize_mod.format_dialog(rows)}\n\n"
             "整理成一条长期记忆，并更新纪要。")}],
        max_tokens=800)
    data = summarize_mod.parse(raw)
    if not data.get("summary"):
        return {"doc_id": doc_id, "skip": "模型没给出 summary"}

    old = {"summary": text[:120], "topics": meta.get("topics") or [],
           "len": len(text or ""), "facts": len(meta.get("key_facts") or [])}
    new = {"summary": data["summary"][:120], "topics": data.get("topics") or [],
           "len": len(data["summary"]), "facts": len(data.get("key_facts") or [])}
    print(f"\n--- {doc_id[:8]} 区间 {rng[0]}:{rng[1]}-{rng[2]}（{len(rows)} 条消息）")
    print(f"  旧 {old['len']} 字 {old['topics']}")
    print(f"     {old['summary']}")
    print(f"  新 {new['len']} 字 {new['topics']}")
    print(f"     {new['summary']}")

    if apply:
        await write_back(core, did, doc_id, text, meta, data)
        print("  已写入（旧版本存进 documents_history）")
    return {"doc_id": doc_id, "old": old, "new": new}


async def write_back(core, did: int, doc_id: str, old_text: str, meta: dict,
                     data: dict) -> None:
    """覆盖 documents / 向量 / 原子。旧行先存 history。"""
    from app_core import memory as memory_mod

    vec = (await core.memory.embed([data["summary"]]))[0]
    c = sqlite3.connect(MEM_DB)
    atoms = c.execute("SELECT content FROM memory_atoms WHERE parent_memory_id=?",
                      (did,)).fetchall()
    c.execute(
        "INSERT INTO documents_history(doc_id, text, metadata, atoms, replaced_at) "
        "VALUES (?,?,?,?,?)",
        (doc_id, old_text, json.dumps(meta, ensure_ascii=False),
         json.dumps([a[0] for a in atoms], ensure_ascii=False), time.time()))
    new_meta = dict(meta)
    new_meta["topics"] = data.get("topics") or []
    new_meta["key_facts"] = data.get("key_facts") or []
    if data.get("followups"):
        new_meta["followups"] = data["followups"]
    new_meta["importance"] = data.get("importance", meta.get("importance", 0.5))
    new_meta["rebuilt_at"] = time.time()
    with c:
        c.execute("UPDATE documents SET text=?, metadata=? WHERE id=?",
                  (data["summary"], json.dumps(new_meta, ensure_ascii=False), did))
        c.execute("DELETE FROM memory_atoms WHERE parent_memory_id=?", (did,))
        for fact in (data.get("key_facts") or []):
            now = time.time()
            c.execute(
                "INSERT INTO memory_atoms(parent_memory_id, atom_type, content, entities, "
                "importance, confidence, created_at, last_accessed_at, last_reinforced_at, "
                "ttl_days, expires_at, status, reinforcement_count, decay_type, "
                "session_id, persona_id, metadata) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (did, "fact", fact, json.dumps(new_meta["topics"], ensure_ascii=False),
                 new_meta["importance"], 0.85, now, now, now, 30.0, now + 30 * 86400,
                 "active", 0, "exponential", meta.get("session_id"),
                 meta.get("persona_id"), "{}"))
        if vec:
            c.execute("INSERT OR REPLACE INTO memory_vectors(doc_id, vec, ts) VALUES (?,?,?)",
                      (doc_id, memory_mod._pack(vec), time.time()))
    c.close()
    core.memory._mat = None
    core.memory._bm25_n = -1


def rollback(doc_id: str) -> None:
    c = sqlite3.connect(MEM_DB)
    row = c.execute(
        "SELECT text, metadata, atoms FROM documents_history WHERE doc_id=? "
        "ORDER BY replaced_at DESC LIMIT 1", (doc_id,)).fetchone()
    if not row:
        print("没有可回滚的历史版本")
        return
    text, meta, atoms = row
    with c:
        c.execute("UPDATE documents SET text=?, metadata=? WHERE doc_id=?",
                  (text, meta, doc_id))
    c.close()
    print(f"已按 {doc_id[:8]} 的上一版还原（向量没动，需要的话重跑 ensure_vectors）")


async def main() -> None:
    from app_core.chat import ChatCore

    apply = "--apply" in sys.argv
    if "--rollback" in sys.argv:
        rollback(sys.argv[sys.argv.index("--rollback") + 1])
        return
    limit = 1
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])
    only = None
    if "--doc" in sys.argv:
        only = sys.argv[sys.argv.index("--doc") + 1]

    c = sqlite3.connect(f"file:{MEM_DB}?mode=ro", uri=True)
    if only:
        rows = c.execute("SELECT id, doc_id, text, metadata FROM documents WHERE doc_id=?",
                         (only,)).fetchall()
    else:
        rows = c.execute("SELECT id, doc_id, text, metadata FROM documents "
                         "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    c.close()

    core = ChatCore()
    done = skipped = 0
    for did, doc_id, text, raw_meta in rows:
        try:
            meta = json.loads(raw_meta or "{}")
        except Exception:
            meta = {}
        if meta.get("status") == "archived":
            continue
        res = await rebuild_one(core, did, doc_id, text or "", meta, apply)
        if res and res.get("skip"):
            skipped += 1
            print(f"  跳过 {doc_id[:8]}：{res['skip']}")
        else:
            done += 1
    print(f"\n=== 重建 {done} 条，跳过 {skipped} 条 ===")
    if not apply:
        print("（dry-run，没有写库；加 --apply 才生效）")


asyncio.run(main())
