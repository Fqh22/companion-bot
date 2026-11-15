#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""export-memory.py —— 把 livingmemory 的记忆库导成框架无关的 JSONL。

为什么：记忆存在独立的 SQLite（/opt/qqbot-data/livingmemory.db），换框架、做离线
分析、或把记忆迁到别处时，直接读库都要先懂表结构。这里每周导出一份「一行一条」
的 JSONL，任何记忆层都能读。

输出（云端）：
    /opt/qqbot-data/workspace/export/memory-YYYYmmdd.jsonl
    /opt/qqbot-data/workspace/export/memory-latest.jsonl
    /opt/qqbot-data/workspace/export/memory-YYYYmmdd.manifest.json

每行格式：{"_table": "documents", "id": 1, ...}
保留最近 8 份。

用法：/opt/nonebot/venv/bin/python ops/export-memory.py（cron 每周日 04:10）
"""
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

DB = "/opt/qqbot-data/livingmemory.db"
OUT = Path("/opt/qqbot-data/workspace/export")
KEEP = 8
# 只导代码实际在用的两张表。
# graph_* / memory_sources 是 AstrBot 时代 livingmemory 插件的结构，
# 2026-10-31 已连同废弃索引一起从库里删掉。
TABLES = ["documents", "memory_atoms"]


def log(msg: str) -> None:
    print("[%s] %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg))


def clean(v):
    """JSON 里尽量放对象，放不进去就退回字符串。"""
    if isinstance(v, (bytes, bytearray)):
        return None
    if isinstance(v, str):
        s = v.strip()
        if s[:1] in "[{" and s[-1:] in "]}":
            try:
                return json.loads(s)
            except Exception:
                return v
    return v


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    day = datetime.now().strftime("%Y%m%d")
    target = OUT / ("memory-%s.jsonl" % day)
    tmp = target.with_suffix(".tmp")

    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    counts = {}
    n_lines = 0

    with tmp.open("w", encoding="utf-8", newline="\n") as f:
        for table in TABLES:
            try:
                cur = c.execute("select * from %s" % table)
            except sqlite3.Error as e:
                log("skip %s: %s" % (table, e))
                continue
            cols = [d[0] for d in cur.description]
            rows = cur.fetchall()
            counts[table] = len(rows)
            for row in rows:
                obj = {"_table": table}
                for k, v in zip(cols, row):
                    obj[k] = clean(v)
                f.write(json.dumps(obj, ensure_ascii=False) + "\n")
                n_lines += 1

    tmp.replace(target)

    manifest = {
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "db": DB,
        "tables": counts,
        "lines": n_lines,
        "file": target.name,
        "bytes": target.stat().st_size,
    }
    (OUT / ("memory-%s.manifest.json" % day)).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")

    latest = OUT / "memory-latest.jsonl"
    try:
        if latest.exists() or latest.is_symlink():
            latest.unlink()
        # 用实文件而非软链：软链进了备份包会断
        latest.write_bytes(target.read_bytes())
    except Exception as e:
        log("warn: latest copy failed: %s" % e)

    # 轮转：只保留最近 KEEP 份
    files = sorted(OUT.glob("memory-2*.jsonl"), reverse=True)
    for old in files[KEEP:]:
        try:
            old.unlink()
        except Exception:
            pass
    manifests = sorted(OUT.glob("memory-2*.manifest.json"), reverse=True)
    for old in manifests[KEEP:]:
        try:
            old.unlink()
        except Exception:
            pass

    log("ok: %s, %d lines, %d bytes, tables=%s" %
        (target.name, n_lines, manifest["bytes"], counts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
