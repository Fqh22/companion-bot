"""全链路重放：拿真实对话重跑一遍，和当时的回复比差异。

只读生产库（复制到临时根再跑），不碰 /opt 的数据。
输出：
  1) 注入一致性：旧 recall 与 Port 召回是否同一条
  2) 回复差异：字符长度比 + 2-gram Jaccard（参考值，不是判死）
  3) 差异最大的 3 条样本
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

SRC = Path("/opt")
TMP = Path(tempfile.mkdtemp(prefix="replay-"))
(TMP / "nonebot").mkdir()
shutil.copytree(SRC / "nonebot/data", TMP / "nonebot/data",
                ignore=shutil.ignore_patterns("files"))
shutil.copytree(SRC / "qqbot-data", TMP / "qqbot-data",
                ignore=shutil.ignore_patterns("meme_packs"))
shutil.copy2(SRC / "nonebot/config.yaml", TMP / "nonebot/config.yaml")

os.environ["QQBOT_ROOT"] = str(TMP)
sys.path.insert(0, str(SRC / "nonebot"))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

from app_core.chat import ChatCore  # noqa: E402
from app_core.memory_port import CoreMemoryPort  # noqa: E402


def jaccard(a: str, b: str) -> float:
    sa = {a[i:i + 2] for i in range(len(a) - 1)}
    sb = {b[i:i + 2] for i in range(len(b) - 1)}
    return len(sa & sb) / max(1, len(sa | sb))


def pairs_from_db(n: int) -> list[tuple[str, str, str]]:
    c = sqlite3.connect(f"file:{TMP / 'nonebot/data/chat.db'}?mode=ro", uri=True)
    rows = c.execute("SELECT id, role, content, session FROM messages ORDER BY id").fetchall()
    c.close()
    out = []
    for i in range(len(rows) - 1):
        a, b = rows[i], rows[i + 1]
        if a[1] != "user" or b[1] != "assistant" or a[3] != b[3]:
            continue
        u, r = (a[2] or "").strip(), (b[2] or "").strip()
        if not (3 <= len(u) <= 60) or not (5 <= len(r) <= 300):
            continue
        if r.startswith(("(", "（", "[")) or "工具执行失败" in r:
            continue
        out.append((a[3], u, r))
    return out[-n:]


async def main() -> int:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    core = ChatCore()
    port = CoreMemoryPort(core)
    samples = pairs_from_db(n)
    print(f"样本 {len(samples)} 条（真实对话）")

    same = 0
    sims = []
    rows = []
    for session, u, old in samples:
        qv = (await core.memory.embed([u]))[0]
        old_ids = [x["doc_id"] for x in await core.memory.recall(u, 5, qv=qv, touch=False)]
        new_ids = [i.ref for i in await port.recall(u, ("fact",), 5, qv=qv, touch=False)]
        if old_ids == new_ids:
            same += 1
        chunks, _imgs = await core.reply(session, u)
        new = "".join(chunks).strip()
        sim = jaccard(old, new)
        sims.append(sim)
        rows.append({"q": u[:30], "old_len": len(old), "new_len": len(new),
                     "sim": round(sim, 2)})

    avg_sim = sum(sims) / max(1, len(sims))
    med_ratio = sorted(r["new_len"] / max(1, r["old_len"]) for r in rows)[len(rows) // 2] if rows else 0
    verdict = ("差异可控" if avg_sim >= 0.25 else "差异偏大，需人工看样本")
    out = {"ts": time.time(), "samples": len(rows), "recall_same": same,
           "avg_sim": round(avg_sim, 3), "median_len_ratio": round(med_ratio, 2),
           "verdict": verdict, "rows": sorted(rows, key=lambda r: r["sim"])[:3]}
    (TMP / "nonebot/data/replay_diff.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"召回一致 {same}/{len(rows)}；平均相似度 {avg_sim:.2f}；长度比中位数 {med_ratio:.2f}")
    print(f"判定：{verdict}")
    for r in out["rows"]:
        print(" 低相似", r)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
