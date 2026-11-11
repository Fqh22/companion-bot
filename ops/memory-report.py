"""记忆使用周报：把 /trace 落盘的轨迹压成一行结论。

用途：观察期每周看一次，判断「召回是不是真的被用上」。
- 0 条比例高 → 召回判据太严，或记忆还没攒够；
- 平均条数接近上限 5 → 可能是「凑满」，要看 0 条以外的质量。
"""
from __future__ import annotations

import json
import os
import sys
import time
from collections import Counter
from pathlib import Path

CODE = Path(os.environ.get("QQBOT_CODE_DIR", "/opt/nonebot")).resolve()
sys.path.insert(0, str(CODE))
os.environ.setdefault("QQBOT_ROOT", "/opt")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

from app_core import paths  # noqa: E402


def main() -> int:
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 7
    path = paths.DATA / "memory_trace.jsonl"
    if not path.exists():
        print("还没有轨迹：data/memory_trace.jsonl（跑几天再来）")
        return 2

    cutoff = time.time() - days * 86400
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            d = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        if float(d.get("ts") or 0) >= cutoff:
            rows.append(d)
    if not rows:
        print(f"最近 {days} 天没有轨迹")
        return 2

    n = len(rows)
    ids = [[i for i in (d.get("ids") or []) if i] for d in rows]
    empty = sum(1 for x in ids if not x)
    avg = sum(len(x) for x in ids) / n
    types = Counter(t.get("type") for d in rows for t in (d.get("trace") or []))
    out = {
        "days": days, "turns": n, "empty": empty,
        "empty_rate": round(empty / n, 3), "avg_items": round(avg, 2),
        "types": dict(types),
    }
    (paths.DATA / "memory_report.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"最近 {days} 天：{n} 轮")
    print(f"  0 条：{empty} 轮（{empty / n:.0%}）")
    print(f"  平均：{avg:.1f} 条/轮")
    print("  类型：" + ("，".join(f"{k}×{v}" for k, v in types.most_common()) or "无"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
