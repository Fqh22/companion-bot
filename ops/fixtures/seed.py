"""给 prompt 快照基线铺最小数据底座（记忆 / 知识 / followup / 画像 / 关系）。

两条硬约束：
1. 不复制 DDL：建表交给 app_core 自己的 init_db；seed 只做写入。
   上游 schema 一变，这里会当场报错，而不是静默写出半截数据。
2. 向量必须和 fake_embed 同源：记忆召回走 numpy 打分，若手填的向量与查询向量不同源，
   算分会不稳或召回为空——槽还是打不亮，却会以为是别的原因。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path


def fake_embed(texts: list[str]) -> list[list[float]]:
    """确定性伪向量：同文本 → 同向量。工具和 seed 共用这一份，保证同源。"""
    out = []
    for t in texts:
        h = hashlib.sha256((t or "").encode("utf-8")).digest()
        out.append([(b - 128) / 128.0 for b in h[:8]])
    return out


def write_files(root: Path, case: dict) -> None:
    """文件类种子：关系小抄 / 画像 / 知识笔记 / 配置。

    写之前断言 root 不是生产目录：relation.md / profile.json 都是真数据，
    写错地方会静默覆盖（2026-11-14 就这么清空过生产的两份）。
    """
    _p = str(root).replace("\\", "/").rstrip("/")
    if _p in ("/opt", "/opt/nonebot", "/", "C:", "") or _p.endswith("/opt/nonebot"):
        raise SystemExit("FATAL 拒绝写生产目录：" + str(root))
    data = root / "nonebot" / "data"
    qdata = root / "qqbot-data"

    rel = data / "relation.md"
    rel.parent.mkdir(parents=True, exist_ok=True)
    rel.write_text(case.get("relation") or "", encoding="utf-8")

    prof = qdata / "workspace" / "learn" / "profile.json"
    prof.parent.mkdir(parents=True, exist_ok=True)
    payload = {"fields": {}, "confidence": {}, "seen": {}}
    fields = case.get("profile") or {}
    if fields:
        payload["fields"] = {k: v for k, v in fields.items() if v}
        payload["confidence"] = {k: 0.9 for k in fields if fields[k]}
        payload["seen"] = {k: 1794700000.0 for k in fields if fields[k]}
    prof.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    notes = case.get("notes") or []
    if notes:
        ndir = qdata / "workspace" / "learn" / "notes"
        ndir.mkdir(parents=True, exist_ok=True)
        for i, note in enumerate(notes):
            p = ndir / f"2026-11-{10 + i:02d}-case-note-{i}.md"
            p.write_text(note, encoding="utf-8")

    cfg = case.get("config") or {}
    lines = [
        "admin:",
        "  user_id: %r" % ("console" if case.get("owner", True) else "someone-else"),
        "llm:",
        "  api: 'http://127.0.0.1:9/v1/chat/completions'",
        "  model: 'fake'",
        "  temperature: 1.0",
        "  reply_deadline_seconds: 45",
        "summary:",
        "  model: 'fake-sum'",
        "chat:",
        "  share_tier: %s" % ("true" if cfg.get("share_tier") else "false"),
        "  style_mode: %s" % (cfg.get("style_mode") or "model"),
        "  shadow_followup: false",
        "profile:",
        "  inject: %s" % (cfg.get("profile_inject") or "shadow"),
        "  shadow_sample: 0.0",
        "context:",
        "  history_max_chars: 6000",
        "  history_max_items: 40",
    ]
    (root / "nonebot" / "config.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")


async def build_db(core, root: Path, case: dict) -> dict:
    """库类种子。返回写入了什么，供报表和断言用。"""
    info = {"memory": 0, "followup": 0, "notes": 0}

    for m in case.get("memory") or []:
        vec = fake_embed([m["text"]])[0]
        core.memory.add_document(
            m["text"], m.get("session") or "private:console",
            float(m.get("importance") or 0.7),
            list(m.get("topics") or []), list(m.get("facts") or []),
            vec=vec, range_key=m.get("range_key") or "")
        info["memory"] += 1

    fu = case.get("followup") or []
    if fu:
        db = root / "nonebot" / "data" / "chat.db"
        c = sqlite3.connect(db)
        with c:
            for row in fu:
                c.execute(
                    "INSERT OR IGNORE INTO followups(text, source, ref_id, created_ts,"
                    " last_asked_ts, ask_count, done, status, status_ts)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (row["text"], "memory", "", 1794700000.0, 0, 0,
                     1 if row.get("status") == "done" else 0,
                     row.get("status") or "todo", 1794700000.0))
                info["followup"] += 1
        c.close()

    notes = case.get("notes") or []
    if notes:
        from app_core import wiki
        ndir = root / "qqbot-data" / "workspace" / "learn" / "notes"
        for p in sorted(ndir.glob("*.md")):
            wiki.upsert_now(p)
            info["notes"] += 1

    return info
