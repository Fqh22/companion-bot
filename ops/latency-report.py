"""从标准输入读取 nonebot journal，按版本、通道和回复形式汇总时延。"""
from __future__ import annotations

import json
import math
import sys
from collections import Counter, defaultdict


def stats(values: list[int]) -> str:
    if not values:
        return "-"
    ordered = sorted(values)
    return "P50 %.2fs / P90 %.2fs (n=%d)" % (
        ordered[math.ceil(len(ordered) * 0.5) - 1] / 1000,
        ordered[math.ceil(len(ordered) * 0.9) - 1] / 1000,
        len(ordered),
    )


def main() -> int:
    groups = defaultdict(list)
    seen = set()
    for line in sys.stdin:
        _, marker, body = line.partition("[latency] ")
        if not marker:
            continue
        try:
            row = json.loads(body)
        except ValueError:
            continue
        if not isinstance(row, dict) or not row.get("rid") or row["rid"] in seen:
            continue
        seen.add(row["rid"])
        route = "tool" if "tool_start_ms" in row else "prefetch" if (
            "search_start_ms" in row or "weather_start_ms" in row) else "direct"
        groups[row.get("release", "unknown"), row.get("platform", "?"),
               row.get("form", "?"), route].append(row)

    for (release, platform, form, route), rows in sorted(groups.items()):
        counts = Counter(row.get("status", "unknown") for row in rows)
        merged = sum(row.get("inputs", 1) > 1 for row in rows)
        print(release, platform, form, route, len(rows), dict(counts), f"merged={merged}")
        for phase in ("input_ready", "batch_start", "intent_finish", "runner_start", "prepare_finish",
                      "model_first", "progress_sent", "first_sent", "finish"):
            values = [row[phase + "_ms"] for row in rows
                      if isinstance(row.get(phase + "_ms"), int) and row[phase + "_ms"] >= 0]
            print("  ", phase, stats(values))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
