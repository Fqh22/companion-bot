"""画像 A/B：注入了和没注入，回复差多少。

同一批真实消息，一组带画像块、一组不带，比长度/句数/是否提到他的近况。
不走 reply()，所以不写库、不动记忆。

用法：
    python replay-profile.py 20
"""
from __future__ import annotations

import asyncio
import json
import re
import statistics
import sys

sys.path.insert(0, "/opt/nonebot")
OUT = "/tmp/profile-ab.json"


def load_samples(n: int) -> list[str]:
    import sqlite3

    c = sqlite3.connect("file:/opt/nonebot/data/chat.db?mode=ro", uri=True)
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
    lens = [len(t) for t in texts]
    segs = [len([x for x in re.split(r"[。！？!?~…\n]", t) if x.strip()]) for t in texts]
    return {
        "平均字数": round(statistics.mean(lens)) if lens else 0,
        "字数中位": round(statistics.median(lens)) if lens else 0,
        "字数标准差": round(statistics.pstdev(lens), 1) if lens else 0,
        "平均句数": round(statistics.mean(segs), 1) if segs else 0,
        "超 40 字": f"{sum(1 for x in lens if x > 40)}/{len(lens)}",
    }


async def run(core, samples, block: str, label: str) -> list[str]:
    outs = []
    for i, text in enumerate(samples, 1):
        msgs = core.build_messages([], text, profile=block)
        try:
            msg = await core.complete_raw(msgs, None, max_tokens=600)
            reply = (msg.get("content") or "").strip()
        except Exception as e:  # noqa: BLE001
            print(f"  [{i}] 失败 {type(e).__name__}")
            continue
        outs.append(reply)
        print(f"  [{i}] {text[:14]!r}\n      → {reply[:60]!r}")
        await asyncio.sleep(0.2)
    return outs


async def main() -> None:
    from app_core import learn, profile as profile_mod
    from app_core.chat import ChatCore

    n = int(sys.argv[1]) if len(sys.argv) > 1 else 12
    samples = load_samples(n)
    core = ChatCore()
    fields = learn.profile_fields()
    conf = learn.profile_conf()
    block = profile_mod.compact(fields, conf) if fields else ""
    print(f"样本 {len(samples)} 条｜画像 {len(fields)} 字段（置信度 {conf}）")
    print(f"画像块（{len(block)} 字）: {block}\n")
    if not block:
        print("没有可注入的画像，退出")
        return

    result = {}
    for label, pb in ((f"带画像", block), ("不带画像", "")):
        print(f"=== {label} ===")
        result[label] = await run(core, samples, pb, label)
        print()

    print("=== 对比 ===")
    ma, mb = metrics(result["带画像"]), metrics(result["不带画像"])
    print("| 指标 | 带画像 | 不带 |")
    print("|---|---|---|")
    for k in ma:
        print(f"| {k} | {ma[k]} | {mb[k]} |")
    json.dump({"block": block, "result": result}, open(OUT, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print("\n明细:", OUT)


asyncio.run(main())
