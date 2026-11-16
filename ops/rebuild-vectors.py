#!/usr/bin/env python3
"""重建记忆向量：换 embedding provider / 维度之后，把旧空间里的向量全部重算。

为什么需要它：换家时只改 config.yaml 的 provider/dim 不够——
memory_vectors 里存的还是旧模型的向量，召回会维度不符并降级成 recent()。
以前 config.example.yaml 注释指向一个不存在的 ops/rebuild-embed.py。

用法（在云端跑，本地缺依赖）：
    /opt/nonebot/venv/bin/python ops/rebuild-vectors.py                 # dry-run
    /opt/nonebot/venv/bin/python ops/rebuild-vectors.py --limit 20      # 只看前 20 条
    /opt/nonebot/venv/bin/python ops/rebuild-vectors.py --apply         # 真的写回

**--apply 会调用 embedding 接口并写生产库**：执行前先跑 ops/cloud-backup.sh
或 ops/deploy.sh 的快照。默认 dry-run，不写任何东西。
"""
from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, "/opt/nonebot")

from app_core.memory import Memory  # noqa: E402
from app_core.vec import pack  # noqa: E402

BATCH = 16


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真的重算并写回（默认 dry-run）")
    ap.add_argument("--limit", type=int, default=10, help="dry-run 展示条数")
    args = ap.parse_args()

    m = Memory()
    if not m.dim:
        print("FAIL：embedding.dim 未配置，先补 config.yaml 再跑")
        return 1
    c: sqlite3.Connection = m._conn()
    rows = c.execute(
        "SELECT v.doc_id, v.vec, d.text FROM memory_vectors v "
        "LEFT JOIN documents d ON d.doc_id = v.doc_id").fetchall()
    lens = Counter(len(r[1]) // 4 for r in rows if r[1])
    stale = [r for r in rows if not r[1] or len(r[1]) // 4 != m.dim or not r[2]]
    print(f"向量 {len(rows)} 条；维度分布 {dict(lens)}；配置维度 {m.dim}")
    print(f"维度不符或缺正文：{len(stale)} 条")
    if not args.apply:
        for doc_id, vec, text in stale[:args.limit]:
            print(f"  {str(doc_id)[:12]}  {len(vec) // 4 if vec else 0} 维  "
                  f"{(text or '')[:44]!r}")
        print("dry-run：未写库。换家后需要重算全部向量，确认后加 --apply")
        return 0

    done = 0
    for i in range(0, len(rows), BATCH):
        batch = rows[i:i + BATCH]
        vecs = await m.embed([(r[2] or "") for r in batch])
        for (doc_id, _, _), vec in zip(batch, vecs):
            if not vec:
                continue
            if len(vec) != m.dim:
                print(f"FAIL：重算得到 {len(vec)} 维，配置为 {m.dim} 维；已停止，未继续写")
                return 1
            c.execute(
                "INSERT OR REPLACE INTO memory_vectors(doc_id, vec, ts) VALUES (?,?,?)",
                (doc_id, pack(vec), time.time()))
            done += 1
        c.commit()
        print(f"  已重建 {done}/{len(rows)}")
    c.close()
    print(f"完成：重建 {done} 条（维度 {m.dim}）")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
