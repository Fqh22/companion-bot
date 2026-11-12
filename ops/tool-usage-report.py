"""工具使用周报：哪些工具真的被调过（用数据决定砍谁）。"""
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
    p = paths.DATA / "tool_usage.jsonl"
    if not p.exists():
        print("还没有工具调用记录（埋点刚加，跑几天再来）")
        return 2
    cutoff = time.time() - days * 86400
    cnt: Counter = Counter()
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            d = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        if float(d.get("ts") or 0) >= cutoff:
            cnt[d.get("tool") or "?"] += 1
    if not cnt:
        print(f"最近 {days} 天没有调用")
        return 2
    total = sum(cnt.values())
    print(f"最近 {days} 天：{total} 次，{len(cnt)} 个工具")
    for name, n in cnt.most_common():
        print(f"{n:5d}  {n / total:5.0%}  {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
