#!/usr/bin/env python3
"""用生产数据验证召回阈值：记忆取运行时 recall_min_score / 0.35（知识库）/ 0.30（表情包）。

2026-11-16：记忆阈值原来写死 0.25，而线上 memory.recall_min_score 早已是 0.35，
探针会给出「阈值很宽松」的错误结论。改成读运行时配置。

诊断脚本（云端 ad-hoc）：只读库 + 调一次 embedding（5 条查询），不写库。
注意：知识库向量表已从 kb_vectors 迁到 kb_chunks（每段一行）。
用法：/opt/nonebot/venv/bin/python ops/probe-recall.py
"""
import asyncio
import sys

sys.path.insert(0, "/opt/nonebot")

from app_core.memory import Memory  # noqa: E402
from app_core.vec import cos as _cos, unpack as _unpack  # noqa: E402

QUERIES = ["面试", "他最近在忙什么", "我喜欢吃什么", "今天好累", "项目进度怎么样"]


async def main() -> None:
    m = Memory()

    import sqlite3

    c = sqlite3.connect("/opt/nonebot/data/chat.db")
    kb_rows = c.execute("SELECT path, vec FROM kb_chunks").fetchall()
    meme_rows = c.execute("SELECT path, vec FROM meme_vectors").fetchall()
    c.close()

    mem_rows = m._all_vectors()

    mem_vecs = [(d, v) for d, v in mem_rows]              # _all_vectors 已解包
    kb_vecs = [(p, _unpack(v)) for p, v in kb_rows]
    meme_vecs = [(p, _unpack(v)) for p, v in meme_rows]

    print(f"样本量：记忆 {len(mem_vecs)} / 知识库 {len(kb_vecs)} / 表情 {len(meme_vecs)}\n")

    for q in QUERIES:
        qv = (await m.embed([q]))[0]
        if not qv:
            print(f"{q!r}: embedding 失败")
            continue

        def top(rows, k=8):
            scored = sorted((_cos(qv, v) for _, v in rows), reverse=True)
            return scored[:k]

        mem = top(mem_vecs)
        kbk = top(kb_vecs)
        mm = top(meme_vecs)
        print(f"query: {q!r}")
        _min = m.recall_min_score
        print("  记忆  : " + " ".join(f"{s:.3f}" for s in mem)
              + f"   → 过 {_min:.2f} 的 {sum(1 for s in mem if s > _min)} 条")
        print("  知识库: " + " ".join(f"{s:.3f}" for s in kbk)
              + f"   → 过 0.35 的 {sum(1 for s in kbk if s >= 0.35)} 条")
        print("  表情包: " + " ".join(f"{s:.3f}" for s in mm)
              + f"   → 过 0.30 的 {sum(1 for s in mm if s >= 0.30)} 条")
        print()


asyncio.run(main())
