"""语气评测台：拿真实用户消息重放，比较不同设置下的回复形状。

用同一批用户消息、同一个人格、同一套提示，只改一个变量（例如 thinking），
跑完算指标并打表。没有这个，每次改语气都是盲调。

用法：
    python replay-tone.py            # 默认 16 条，跑 thinking on / casual-off 两组
    python replay-tone.py 30         # 指定条数
"""
from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import statistics
import sys
import time

sys.path.insert(0, "/opt/nonebot")

DB = "/opt/nonebot/data/chat.db"
OUT = "/tmp/tone-replay.json"


def load_samples(n: int) -> list[str]:
    c = sqlite3.connect(DB)
    rows = c.execute(
        "SELECT content FROM messages WHERE role='user' AND session LIKE 'private:%'"
        " ORDER BY id DESC LIMIT 400").fetchall()
    c.close()
    out = []
    for (t,) in rows:
        t = (t or "").strip()
        if 2 <= len(t) <= 120 and not t.startswith("[") and t not in out:
            out.append(t)
        if len(out) >= n:
            break
    return out


def metrics(texts: list[str]) -> dict:
    if not texts:
        return {}
    segs, lens, cvs = [], [], []
    for t in texts:
        parts = [x for x in re.split(r"[。！？!?~…]", t) if x.strip()]
        segs.append(len(parts))
        lens.append(len(t))
        if len(parts) > 1:
            ls = [len(x) for x in parts]
            m = statistics.mean(ls)
            cvs.append(statistics.pstdev(ls) / m if m else 0)
    n = len(texts)
    return {
        "样本": n,
        "平均字数": round(statistics.mean(lens)),
        "字数中位": round(statistics.median(lens)),
        "平均句数": round(statistics.mean(segs), 1),
        "三段以上": f"{sum(1 for s in segs if s >= 3)}/{n}",
        "问句结尾": f"{sum(1 for t in texts if t.rstrip().endswith(('？', '?')))}/{n}",
        "枚举(第一/一是)": f"{sum(1 for t in texts if re.search(r'第[一二三]|一是|二是', t))}/{n}",
        "句长变异": round(statistics.mean(cvs), 2) if cvs else 0,
        "含建议词": f"{sum(1 for t in texts if any(w in t for w in ('建议', '可以试试', '不妨', '最好')))}/{n}",
    }


async def run_variant(core, samples: list[str], thinking: bool | None) -> list[str]:
    from app_core import prompt as prompt_mod

    outs = []
    for i, text in enumerate(samples, 1):
        msgs = core.build_messages([], text, [], "", "", [], None, [], "")
        try:
            msg = await core.complete_raw(msgs, None, max_tokens=700, thinking=thinking)
            reply = (msg.get("content") or "").strip()
        except Exception as e:  # noqa: BLE001
            print(f"  [{i}] 失败 {type(e).__name__}")
            continue
        reply, _ = prompt_mod.strip_advice_tail(reply)
        outs.append(reply)
        print(f"  [{i}] {len(reply)}字 {reply[:40]}")
        await asyncio.sleep(0.3)
    return outs


async def main() -> None:
    from app_core.chat import ChatCore

    n = int(sys.argv[1]) if len(sys.argv) > 1 else 16
    samples = load_samples(n)
    print(f"样本 {len(samples)} 条\n")
    core = ChatCore()
    result = {}
    for name, think in (("思考开（现状）", None), ("闲聊关思考", False)):
        print(f"=== {name} ===")
        outs = await run_variant(core, samples, think)
        result[name] = {"metrics": metrics(outs), "texts": outs}
        print()
    print("=== 对比 ===")
    keys = list(next(iter(result.values()))["metrics"].keys())
    print("| 指标 | " + " | ".join(result.keys()) + " |")
    print("|---|" + "---|" * len(result))
    for k in keys:
        print(f"| {k} | " + " | ".join(str(v["metrics"].get(k)) for v in result.values()) + " |")
    json.dump(result, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("\n明细:", OUT)


asyncio.run(main())
