"""人格 A/B：把人格砍到 200 字，回复会差多少。

用来回答一个具体问题：**主人那条链路的人格，是在承担「声音」，还是只是装饰？**

做法：同一批真实用户消息，用两套人格各跑一遍，比长度、句数、开头多样性、
以及「像不像同一个人在说话」。不走 reply()——直接 build_messages + complete_raw，
所以不写库、不动记忆、不触发总结。

用法：
    python replay-persona.py 20
"""
from __future__ import annotations

import asyncio
import json
import re
import statistics
import sys

sys.path.insert(0, "/opt/nonebot")

DB = "/opt/nonebot/data/chat.db"
OUT = "/tmp/persona-ab.json"

# 对照组：只留「短、不总结、别油」三条
MINIMAL = {
    "system_prompt": (
        "你说话很短，像在 QQ 上打字，一句到两句。"
        "不总结、不升华、不给人生建议，不说「希望对你有帮助」这类话。"
        "不油：不抖机灵、不堆语气词、不为显得有趣而跑题。"
    ),
    "begin_dialogs": [],
}


def load_samples(n: int) -> list[str]:
    import sqlite3

    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    rows = c.execute(
        "SELECT content FROM messages WHERE role='user' AND session LIKE 'private:%'"
        " ORDER BY id DESC LIMIT 600").fetchall()
    c.close()
    out = []
    for (t,) in rows:
        t = (t or "").strip()
        if 2 <= len(t) <= 100 and not t.startswith("[") and not t.startswith("/") and t not in out:
            out.append(t)
        if len(out) >= n:
            break
    return out


def metrics(texts: list[str]) -> dict:
    if not texts:
        return {}
    lens = [len(t) for t in texts]
    segs = [len([x for x in re.split(r"[。！？!?~…\n]", t) if x.strip()]) for t in texts]
    heads = [t[:2] for t in texts]
    return {
        "条数": len(texts),
        "平均字数": round(statistics.mean(lens)),
        "字数中位": round(statistics.median(lens)),
        "字数标准差": round(statistics.pstdev(lens), 1),
        "平均句数": round(statistics.mean(segs), 1),
        "单句占比": f"{sum(1 for s in segs if s <= 1)}/{len(segs)}",
        "开头重复率": f"{len(heads) - len(set(heads))}/{len(heads)}",
        "超 40 字": f"{sum(1 for x in lens if x > 40)}/{len(lens)}",
        "总结腔": sum(1 for t in texts if re.search(r"总之|总的来说|希望能|希望对|祝", t)),
        "客服腔": sum(1 for t in texts if re.search(r"有需要|随时|帮到你|可以试试", t)),
    }


async def run(core, persona: dict, samples: list[str], label: str) -> list[str]:
    outs = []
    for i, text in enumerate(samples, 1):
        msgs = core.build_messages([], text, persona=persona)
        try:
            msg = await core.complete_raw(msgs, None, max_tokens=600)
            reply = (msg.get("content") or "").strip()
        except Exception as e:  # noqa: BLE001
            print(f"  [{i}] 失败 {type(e).__name__}: {e}")
            continue
        outs.append(reply)
        print(f"  [{i}] {text[:14]!r} → {reply[:46]!r}")
        await asyncio.sleep(0.2)
    return outs


async def main() -> None:
    from app_core.chat import ChatCore

    n = int(sys.argv[1]) if len(sys.argv) > 1 else 12
    samples = load_samples(n)
    print(f"样本 {len(samples)} 条\n")
    core = ChatCore()

    result = {}
    for label, persona in (("A 现人格", core.persona), ("B 砍到三条", MINIMAL)):
        print(f"=== {label} ===")
        texts = await run(core, persona, samples, label)
        result[label] = {"metrics": metrics(texts), "texts": texts}
        print()

    print("=== 对比 ===")
    keys = list(next(iter(result.values()))["metrics"].keys())
    print("| 指标 | " + " | ".join(result) + " |")
    print("|---|" + "---|" * len(result))
    for k in keys:
        print(f"| {k} | " + " | ".join(str(v["metrics"].get(k)) for v in result.values()) + " |")

    json.dump({"samples": samples, "result": result},
              open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("\n明细:", OUT)


asyncio.run(main())
