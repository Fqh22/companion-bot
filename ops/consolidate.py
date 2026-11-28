#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""碎片整合候选报告（**只读**，不是整合器）。

汇总退役实验留下的历史日志。统计候选对与相似度，保留人工复核能力。

用法：
    python ops/consolidate.py                       # 读默认 data 目录
    python ops/consolidate.py --data /tmp/x --top 10
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

DEFAULT_DATA = Path("/opt/nonebot/data")
LOG_NAME = "consolidation-candidates.jsonl"


def report(log: Path) -> dict:
    """只读统计；路径可注入，缺文件时返回全 0 而不是抛。"""
    rows: list[dict] = []
    if log.exists():
        for raw in log.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                r = json.loads(raw)
            except Exception:  # noqa: BLE001
                continue
            if isinstance(r, dict):
                rows.append(r)

    pairs: dict[tuple, dict] = {}
    for r in rows:
        new = " ".join(str(r.get("new") or "").split())
        key = (str(r.get("old_id") or ""), new)
        try:
            cos = float(r.get("cos") or 0)
        except (TypeError, ValueError):
            cos = 0.0
        if key not in pairs or cos > pairs[key]["cos"]:
            pairs[key] = {
                "cos": cos,
                "new": new,
                "old_id": key[0],
                "old_text": " ".join(str(r.get("old_text") or "").split()),
                "same_text": new and new == " ".join(
                    str(r.get("old_text") or "").split()),
            }

    ranked = sorted(pairs.values(), key=lambda x: -x["cos"])
    return {
        "samples": len(rows),
        "pairs": len(ranked),
        "high": sum(1 for p in ranked if p["cos"] >= 0.90),
        "mid": sum(1 for p in ranked if 0.85 <= p["cos"] < 0.90),
        "low": sum(1 for p in ranked if p["cos"] < 0.85),
        "same_text": sum(1 for p in ranked if p["same_text"]),
        "top": ranked[:20],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="碎片整合候选报告（只读）")
    ap.add_argument("--data", type=Path, default=DEFAULT_DATA)
    ap.add_argument("--top", type=int, default=5)
    args = ap.parse_args()
    r = report(args.data / LOG_NAME)
    print("=== 碎片整合候选（只读，不写库）===")
    print(f"样本 {r['samples']} 条 / 去重后 {r['pairs']} 对")
    print(f"cos ≥0.90：{r['high']}　0.85–0.90：{r['mid']}　<0.85：{r['low']}")
    print(f"文本完全相同：{r['same_text']} 对（最安全的一类，可只归档后写的那条）")
    for p in r["top"][:max(0, args.top)]:
        tag = "同文" if p["same_text"] else "近似"
        print(f"  [{tag} {p['cos']:.3f}] {p['old_id'][:8]} 旧：{p['old_text'][:48]}")
        print(f"                  新：{p['new'][:48]}")
    print("生成自足条目需调模型且不可逆，本批不做；判据与截止日见 docs/04-评测与实验.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
