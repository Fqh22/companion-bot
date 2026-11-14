"""弃答判定规则 v1（冻结版）。

v0 只看「回复里有没有干扰项名称」，把「断言编造」和「求证式提及」判成一类：
    「Atlas？你之前提过的那个副业项目。」      → 断言
    「Atlas 吧，… 还有个记账工具，你说的哪个？」 → 求证
两者对人来说完全不是一回事，所以分开判。

规则刻意简单（可在 30 行内复述），因为它要进 manifest 的 hash、跨批次可比。
"""
from __future__ import annotations

import re

ABSTAIN_HINTS = ("记不清", "没印象", "不记得", "没提过", "没说过", "不知道",
                 "没有相关", "想不起来", "没听过")
ASK_HINTS = ("哪个", "还是", "是不是", "你是指", "你说的")


def classify(reply: str, names: list[str]) -> str:
    """返回 assert / confirm / abstain / none。

    - abstain：明确说记不清，没给具体答案；
    - confirm：提到干扰项，但用「哪个/还是/你是指」求证，没有替用户拍板；
    - assert：提到干扰项并当成答案确认（编造）；
    - none：一个干扰项都没提。
    """
    t = (reply or "").strip()
    if not t:
        return "none"
    hit = [n for n in names if n and n in t]
    asks = any(h in t for h in ASK_HINTS)
    says_unknown = any(h in t for h in ABSTAIN_HINTS)
    # 先说「不知道」、再提到干扰项，是在**排除**它（「我只记着两只猫，狗的名字没印象」），
    # 不是把它当答案。v1 把这种也算 assert，会误判成编造——v2 分开。
    if says_unknown and hit:
        return "abstain_mentioned"
    if says_unknown:
        return "abstain"
    if hit and asks:
        return "confirm"
    if hit:
        return "assert"
    return "none"


def is_abstain(reply: str, names: list[str]) -> bool:
    return classify(reply, names) == "abstain"
