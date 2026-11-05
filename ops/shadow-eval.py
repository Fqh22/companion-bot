"""影子样本评估：把攒下来的 jsonl 变成一张能看懂的账。

两个影子各自回答一个问题：

- `shadow-retrieval.jsonl`：关键词通道（BM25+RRF）值不值得转正。
  看 overlap——**全重合说明两路在捞同一批东西，白干；重合度低才说明
  关键词真的捞回了向量捞不到的。**
- `shadow-graph.jsonl`：共现实体连边有没有料。
  看 expanded 非空的比例，以及非空的那些到底连出了几条。

用法（云端，只读）：
    python shadow-eval.py            # 全部样本
    python shadow-eval.py --days 7   # 只看最近 7 天
    python shadow-eval.py --top 10   # 每类多打印几条例样
"""
from __future__ import annotations

import json
import sys
import time
from collections import Counter
from pathlib import Path

DATA = Path("/opt/nonebot/data")
RETRIEVAL = DATA / "shadow-retrieval.jsonl"
GRAPH = DATA / "shadow-graph.jsonl"
PROFILE = DATA / "shadow-profile.jsonl"
SECOND = DATA / "shadow-second.jsonl"


def load(path: Path, days: float | None) -> list[dict]:
    if not path.exists():
        return []
    since = time.time() - days * 86400 if days else 0
    out = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if rec.get("ts", 0) < since:
            continue
        out.append(rec)
    return out


def pct(a: int, b: int) -> str:
    return f"{a / b * 100:.0f}%" if b else "-"


def eval_retrieval(rows: list[dict], top: int) -> None:
    n = len(rows)
    print(f"\n=== 关键词通道（{n} 次召回） ===")
    if not n:
        print("  还没有样本")
        return
    ov = Counter(r.get("overlap", 0) for r in rows)
    print("  overlap 分布（与 dense 重合几条）:",
          "  ".join(f"{k}:{ov.get(k, 0)}" for k in range(6)))
    low = sum(v for k, v in ov.items() if k < 4)
    print(f"  有差异（overlap<4）: {low}/{n} = {pct(low, n)}")
    # 关键词捞回来、dense 完全没有的记录
    bring = [r for r in rows if set(r.get("bm25") or []) - set(r.get("dense") or [])]
    print(f"  关键词额外捞到新记录: {len(bring)}/{n} = {pct(len(bring), n)}")
    for r in bring[:top]:
        new = set(r.get("bm25") or []) - set(r.get("dense") or [])
        print(f"    · {r.get('q', '')[:28]!r} 新增 {sorted(new)}")


def eval_graph(rows: list[dict], top: int) -> None:
    n = len(rows)
    print(f"\n=== 实体连边（{n} 次召回） ===")
    if not n:
        print("  还没有样本")
        return
    nonempty = [r for r in rows if r.get("expanded")]
    print(f"  能连出邻居: {len(nonempty)}/{n} = {pct(len(nonempty), n)}")
    if nonempty:
        cnt = Counter(len(r["expanded"]) for r in nonempty)
        print("  每次连出几条:", "  ".join(f"{k}条:{v}" for k, v in sorted(cnt.items())))
    for r in nonempty[:top]:
        print(f"    · {r.get('q', '')[:28]!r} 连到 {r['expanded']}")


def eval_profile(rows: list[dict], top: int) -> None:
    n = len(rows)
    print(f"\n=== 画像注入（{n} 次） ===")
    if not n:
        print("  还没有样本（部署后还没有新的对话）")
        return
    modes = Counter(r.get("mode", "-") for r in rows)
    print("  模式分布:", dict(modes))
    block = rows[-1].get("block") or ""
    print(f"  最近一次要注入的块（{len(block)} 字）: {block[:80]}…")
    lens = [len(r.get("block") or "") for r in rows]
    print(f"  块长度：中位 {sorted(lens)[len(lens)//2]} 字，最短 {min(lens)}，最长 {max(lens)}")


def eval_second(rows: list[dict], top: int) -> None:
    n = len(rows)
    print(f"\n=== 二次召回（{n} 次触发） ===")
    if not n:
        print("  还没有样本（refers_to_previous 没命中过，或部署后没对话）")
        return
    hit = sum(1 for r in rows if r.get("got"))
    print(f"  能捞到东西: {hit}/{n} = {pct(hit, n)}")
    for r in rows[-top:]:
        print(f"    · 他这句 {r.get('said','')[:18]!r} ← 上一句 {r.get('prev','')[:18]!r} "
              f"→ {r.get('got')}")


def main() -> None:
    days = None
    top = 3
    if "--days" in sys.argv:
        days = float(sys.argv[sys.argv.index("--days") + 1])
    if "--top" in sys.argv:
        top = int(sys.argv[sys.argv.index("--top") + 1])

    print(f"样本目录：{DATA}" + (f"（最近 {days:g} 天）" if days else "（全部）"))
    eval_retrieval(load(RETRIEVAL, days), top)
    eval_graph(load(GRAPH, days), top)
    eval_profile(load(PROFILE, days), top)
    eval_second(load(SECOND, days), top)
    print("\n判读建议：召回看「有差异」≥15%，连边看「能连出邻居」≥50%，"
          "画像看块长度是否稳定在 120–200 字，二次召回看「能捞到」的比例是否明显高于首次召回。")


main()
