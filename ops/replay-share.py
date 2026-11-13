"""分享轮 A/B：同一批图，比「不加档位」与「加档位上限」两组的回复形状。

用法（云端）：python ops/replay-share.py 16
输出：/tmp/share-replay.json + /tmp/share-replay.md（UTF-8，避开控制台编码）

只测两件事：注入档位上限后，**动机越界**是否下降、**指认**有没有丢。
功能点命中率仍需人判——这是机器指标的边界。
"""
from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import statistics
import sys

sys.path.insert(0, "/opt/nonebot")

DB = "/opt/nonebot/data/chat.db"
OUT_JSON = "/tmp/share-replay.json"
OUT_MD = "/tmp/share-replay.md"

# 动机越界：把话说到「他这人 / 他为什么」上。词表按历史真实形态补，
# 不是穷举——它只用来量趋势，不参与判定。
OVER = ("为了", "所以你是", "你其实", "怪不得", "说明你", "你这种", "你一直",
        "你就是", "你大概", "我猜你", "看来你", "你心里", "懂你", "配你",
        "跟刚聊的", "你会", "你也", "你俩")
SELF_CHECK = re.compile(r"^\s*\[自检\][^\n]*\n?", re.M)
CAPTION = re.compile(r"｜图注：[^\n]*")
ATTACH = re.compile(r"（附了[^）]*）")
PLACE = re.compile(r"^\[发了图片\]")


def strip_self(text: str) -> str:
    """去掉自检标签行：模型输出它，线上会解析掉，不进最终回复。"""
    return SELF_CHECK.sub("", text).strip()


def load(n: int):
    """只取「无字 + 有图注」的分享轮：有字或被当成求助的都不算。"""
    c = sqlite3.connect(DB)
    rows = c.execute(
        "SELECT content FROM messages WHERE role='user' AND content LIKE '%附了%'"
        " ORDER BY id DESC LIMIT 600").fetchall()
    c.close()
    out, seen = [], set()
    for (t,) in rows:
        t = t or ""
        if t in seen:
            continue
        seen.add(t)
        if not caption_of(t):
            continue          # 没图注：判不了体裁，也复现不了当轮
        if said(t):
            continue          # 他配了字：不是「无授权」样本
        out.append(t)
        if len(out) >= n:
            break
    return out


def caption_of(text: str) -> str:
    m = CAPTION.search(text)
    return m.group(0).replace("｜图注：", "", 1).strip() if m else ""


def said(text: str) -> str:
    return ATTACH.sub("", CAPTION.sub("", PLACE.sub("", text))).strip().strip("｜ \n")


def metrics(texts):
    texts = [strip_self(t) for t in texts if strip_self(t)]
    if not texts:
        return {}
    n = len(texts)
    lens = [len(t) for t in texts]
    return {
        "样本": n,
        "平均字数": round(statistics.mean(lens)),
        "字数中位": round(statistics.median(lens)),
        "问句结尾": f"{sum(1 for t in texts if t.rstrip().endswith(('？', '?')))}/{n}",
        "动机越界条数": f"{sum(1 for t in texts if any(w in t for w in OVER))}/{n}",
        "越界词总数": sum(sum(t.count(w) for w in OVER) for t in texts),
        "含建议词": f"{sum(1 for t in texts if any(w in t for w in ('建议', '可以试试', '不妨', '最好')))}/{n}",
    }


async def main() -> None:
    from app_core import chat as chat_mod
    from app_core import share_tier

    n = int(sys.argv[1]) if len(sys.argv) > 1 else 16
    samples = load(n)
    print(f"样本 {len(samples)} 条（无字 + 有图注）\n")

    groups = {}
    for name in ("不加档位", "加档位上限"):
        outs, tiers = [], []
        print(f"=== {name} ===")
        core = chat_mod.ChatCore()
        for i, text in enumerate(samples, 1):
            cap = caption_of(text)
            extra = ["[图注] " + cap + "（这是图注，不是原图；细节不确定就别硬说）"]
            if name == "加档位上限":
                t, why = share_tier.judge(text, cap)
                tiers.append(t)
                extra.append(share_tier.block(t, why))
            msgs = core.build_messages([], said(text), extra_system=extra,
                                       style_mode="model")
            try:
                msg = await core.complete_raw(msgs, None, max_tokens=700)
                reply = strip_self((msg.get("content") or "").strip())
            except Exception as e:  # noqa: BLE001
                print(f"  [{i}] 失败 {type(e).__name__}: {e}")
                continue
            outs.append(reply)
            print(f"  [{i}] {len(reply)}字")
            await asyncio.sleep(0.3)
        groups[name] = {"metrics": metrics(outs), "texts": outs, "tiers": tiers}
        print()

    keys = list(groups["不加档位"]["metrics"].keys())
    lines = ["| 指标 | " + " | ".join(groups) + " |",
             "|---|" + "---|" * len(groups)]
    for k in keys:
        lines.append(f"| {k} | " + " | ".join(str(g["metrics"].get(k)) for g in groups.values()) + " |")

    lines.append("\n## 逐条对照（A=不加档位 / B=加档位）\n")
    a = groups["不加档位"]["texts"]
    b = groups["加档位上限"]["texts"]
    ti = groups["加档位上限"]["tiers"]
    for i in range(min(len(a), len(b))):
        lines.append(f"### {i + 1} · 档={ti[i] if i < len(ti) else '?'}")
        lines.append(f"- A（{len(a[i])}）: {a[i]}")
        lines.append(f"- B（{len(b[i])}）: {b[i]}")
        lines.append("")

    out = "\n".join(lines)
    open(OUT_MD, "w", encoding="utf-8").write(out)
    json.dump(groups, open(OUT_JSON, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(out)
    print("\n明细:", OUT_MD)


asyncio.run(main())
