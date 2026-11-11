"""回话自检周报：主类型分布 / 依据=无比例 / 来源分布。

用途：一周看一次，判断「它是不是经常没依据就下判断」（空中楼阁的量化版）。
"""
from __future__ import annotations

import json
import os
import re
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
    p = paths.DATA / "self_check.jsonl"
    if not p.exists():
        print("还没有自检记录（跑几天再来）")
        return 2
    cutoff = time.time() - days * 86400
    rows: list[dict] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            d = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        if float(d.get("ts") or 0) >= cutoff:
            rows.append(d)
    if not rows:
        print(f"最近 {days} 天没有记录")
        return 2

    n = len(rows)
    prim: Counter = Counter()
    src: Counter = Counter()
    noev = 0
    for d in rows:
        t = str(d.get("tag") or "")
        m = re.search(r"主\s*=\s*([^\s|]+)", t)
        if m:
            prim[m.group(1)] += 1
        m2 = re.search(r"来源\s*=\s*(\S+)", t)
        if m2:
            src[m2.group(1)] += 1
        if re.search(r"依据\s*=\s*无", t):
            noev += 1

    print(f"最近 {days} 天：{n} 条")
    print("主类型：" + ("，".join(f"{k}×{v}" for k, v in prim.most_common()) or "无"))
    print("来源：" + ("，".join(f"{k}×{v}" for k, v in src.most_common()) or "无"))
    print(f"依据=无：{noev} 条（{noev / n:.0%}）")

    out = {"days": days, "turns": n, "primary": dict(prim),
           "source": dict(src), "no_evidence": noev}
    (paths.DATA / "self_check_report.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
