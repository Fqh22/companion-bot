"""影子判断：契约路径 vs 旧路径的召回一致性 + 空结果率。

不改变任何行为（touch=False）。结论写到 data/memory_shadow.json，供下一步决策：
- 一致率 100% 且空结果率 < 40% → 契约稳定，可继续迁 profile / 入口。
- 空结果率高 → 先调 recall_min_score / 时间衰减，再迁。
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

CODE = Path(os.environ.get("QQBOT_CODE_DIR", "/opt/nonebot")).resolve()
sys.path.insert(0, str(CODE))
os.environ.setdefault("QQBOT_ROOT", "/opt")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

from app_core import paths  # noqa: E402
from app_core.chat import ChatCore  # noqa: E402
from app_core.memory_port import CoreMemoryPort  # noqa: E402


async def main() -> int:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    core = ChatCore()
    port = CoreMemoryPort(core)

    c = sqlite3.connect(f"file:{paths.DATA / 'chat.db'}?mode=ro", uri=True)
    rows = c.execute(
        "SELECT content FROM messages WHERE role='user' ORDER BY id DESC LIMIT ?",
        (n * 4,)).fetchall()
    c.close()
    qs = [r[0].strip() for r in rows
          if r[0] and 2 <= len(r[0].strip()) <= 80][:n]
    if not qs:
        print("没有可用的样本")
        return 2

    same = diff = empty = 0
    counts = []
    diffs = []
    for q in qs:
        try:
            qv = (await core.memory.embed([q]))[0]
            old = await core.memory.recall(q, 5, qv=qv, touch=False)
            new = await port.recall(q, ("fact",), 5, qv=qv, touch=False)
            a = [x["doc_id"] for x in old]
            b = [x.ref for x in new]
            counts.append(len(b))
            if not b:
                empty += 1
            if a == b:
                same += 1
            else:
                diff += 1
                diffs.append({"q": q[:30], "old": len(a), "new": len(b)})
        except Exception as e:  # noqa: BLE001
            diff += 1
            diffs.append({"q": q[:30], "err": type(e).__name__})

    total = len(counts) or 1
    empty_rate = round(empty / len(qs), 3)
    avg = round(sum(counts) / total, 2)
    verdict = ("契约一致" if diff == 0 else "契约有差异")
    next_step = ("可继续迁 profile / 入口" if diff == 0 and empty_rate < 0.4
                 else "先查召回判据（空结果率高）" if empty_rate >= 0.4
                 else "先修差异")
    out = {
        "ts": time.time(), "samples": len(qs), "same": same, "diff": diff,
        "empty": empty, "empty_rate": empty_rate, "avg_items": avg,
        "verdict": verdict, "next": next_step, "diffs": diffs[:5],
    }
    paths.DATA.mkdir(parents=True, exist_ok=True)
    (paths.DATA / "memory_shadow.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"samples={len(qs)} same={same} diff={diff}")
    print(f"empty={empty} ({empty_rate:.0%}) avg_items={avg}")
    print(f"verdict: {verdict} → {next_step}")
    return 0 if diff == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
