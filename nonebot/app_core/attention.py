"""注意力：一条回复是在说他，还是在说自己。

**为什么要量它**：陪伴的体感不是「这 AI 真有意思」，而是「它在听我说话」。
但「有意思」和「在听」在模型这里是有竞争的——一个会抖机灵、有脾气的回复
读起来更像人，也更容易让人（尤其是旁观者）觉得好。
没有指标的话，这种漂移是不可见的：每次改动都"感觉更生动了一点"。

**指标定义**：自指率 = 我这边的话 ÷（我这边 + 你那边）。

用「分句归属」而不是数 "我" 出现几次：一句「我觉得你该投运营岗」里既有我也有你，
但它是**在说他**，不该算成自指。所以按分句算：这一句的主语/重心在谁。

**只是记账，不改回复**。先攒两周，看它是不是在往上走。
"""
from __future__ import annotations

import re

_SELF = ("我觉得", "我想", "我最近", "我这边", "我可", "我倒是", "说真的，我",
         "我自己", "我其实", "我比较", "我不太", "我会")
_YOU = ("你", "您", "咱")
_SPLIT = re.compile(r"[，。！？；、\n~…,.!?;]+")


def _clauses(text: str) -> list[str]:
    return [c.strip() for c in _SPLIT.split(text or "") if c.strip()]


def self_ref_ratio(reply: str) -> dict:
    """返回 {ratio, self, you, clauses}。

    ratio ∈ [0,1]：0 全在说他，1 全在说自己。没有可判定的分句时返回 None。
    """
    clauses = _clauses(reply)
    me = you = 0
    for c in clauses:
        has_self = c.startswith("我") or any(w in c for w in _SELF)
        has_you = any(w in c for w in _YOU)
        if has_you:
            you += 1          # 提到他就算在说他，哪怕句子以「我」开头
        elif has_self:
            me += 1
    total = me + you
    return {
        "ratio": round(me / total, 2) if total else None,
        "self": me,
        "you": you,
        "clauses": len(clauses),
    }
