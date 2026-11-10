"""[domain] 文本相似度：两个地方要用（提醒去重、跟进去重）。

单独抽出来而不是各写一份，是因为这里有个**反直觉的点**：
只算整体相似度会漏掉最常见的一类重复——「一句话是另一句的加强版」。
实测真实重复对整体相似度只有 0.64，而短句 bigram 覆盖率能到 0.73；
无关对则一个是 0.29、一个是 0.00。所以取两者的较大值。
"""
from __future__ import annotations

import difflib
import re

_PUNCT = re.compile(r"[\s，。、；：！？「」“”\"'（）()\-—…~,.;:!?]+")


def norm(s: str) -> str:
    return _PUNCT.sub("", (s or "").lower())


def similar(a: str, b: str) -> float:
    """0–1。取「整体相似」与「短句 bigram 覆盖率」的较大值。

    **短文本只认完全相同**：5 个字以下的条目（「老待办1」「买牛奶」）
    模糊比对会把不同的事判成重复——之前「问题一/问题二」就这么被误合并过。
    """
    x, y = norm(a), norm(b)
    if not x or not y:
        return 0.0
    if x == y:
        return 1.0
    if min(len(x), len(y)) < 5:
        return 0.0
    ratio = difflib.SequenceMatcher(None, x, y).ratio()
    bx = {x[i:i + 2] for i in range(len(x) - 1)}
    by = {y[i:i + 2] for i in range(len(y) - 1)}
    contain = len(bx & by) / min(len(bx), len(by)) if bx and by else 0.0
    return max(ratio, contain)
