#!/usr/bin/env python3
"""离线现实采样：给「见证句」提供证据。只读，不生成、不发送。

用法：
    python ops/reality-sample.py            # 最近 7 天
    python ops/reality-sample.py 14         # 最近 14 天

输出两组：统计（次数 + 时间）和候选句。候选句只是模板，剂量必须在
ops/drill-console.py 里人工试——像见证，不像监控，才算过。
"""
from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "nonebot"))
from app_core.reality import GROUPS, _NOT  # noqa: E402

DB = Path(os.environ.get("QQBOT_CHAT_DB") or "/opt/nonebot/data/chat.db")


def hits(days: int) -> dict[str, list[tuple[str, str]]]:
    since = (datetime.now() - timedelta(days=days)).timestamp()
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=10)
    rows = c.execute(
        "SELECT content, ts FROM messages WHERE role='user' AND ts>=? ORDER BY id",
        (since,)).fetchall()
    c.close()
    out = {k: [] for k in GROUPS}
    for text, ts in rows:
        s = (text or "").strip()
        if not s or s.startswith("/"):
            continue
        if any(w in s for w in _NOT):
            continue
        # 图注是模型写的，不是他说的
        if s.startswith("[发了图片]"):
            continue
        t = datetime.fromtimestamp(float(ts)).strftime("%m-%d %H:%M")
        for name, words in GROUPS.items():
            if any(w in s for w in words):
                out[name].append((t, s[:40]))
                break
    return out


def main() -> int:
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 7
    if not DB.exists():
        print("库不存在：", DB)
        return 2
    data = hits(days)
    print(f"== 最近 {days} 天现实痕迹 ==")
    for name, items in data.items():
        print(f"{name}: {len(items)} 次")
        for t, s in items[-5:]:
            print(f"  {t}  {s}")
    print()
    print("== 候选见证句（去 ops/drill-console.py 试剂量）==")
    print()
    for name, items in data.items():
        if not items:
            continue
        times = "、".join(t for t, _ in items[-3:])
        print(f"- 你最近 {days} 天说了 {len(items)} 次{name}（{times}）。")
    if not any(data.values()):
        print("- （没有足够样本，先别造句子）")
    return 0


if __name__ == "__main__":
    sys.exit(main())