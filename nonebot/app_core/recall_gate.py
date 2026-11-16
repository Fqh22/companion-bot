"""[domain] 检索门控：这一轮要不要动用记忆。

为什么要判：真实陪伴流量里只有约 3.4% 的消息真的需要回忆（RealCompanion，
2026-10），而「要不要检索」的检测器在真实消息上 AUROC 仅 0.530——接近随机。
不加选择地每轮全库扫描再注入，最容易产生的不是「记不住」，而是过度引用、
把陈年旧事翻出来当话题。瓶颈是「什么时候不该检索」，不是「怎么检索」。

**保守取向**：拿不准一律 `need=True`。门控误杀一轮的代价（该记得没记）
远大于多召回一轮（只多一点 token）。这里只做纯规则、不调模型，也不碰 IO：
不开影子时零开销；shadow 只落盘判断与实际召回条数；on 才真的跳过 fact 召回。

影子实验，截止 2026-11-30，判据见 docs/04-评测与实验.md。
"""
from __future__ import annotations

import re

# 明确的回忆请求。宁窄勿宽：这些词出现时几乎一定需要翻旧账。
_EXPLICIT = (
    "还记得", "记不记得", "记得吗", "你说过", "我说过", "我跟你提过",
    "我们聊过", "上次", "上回", "前阵子", "那时候", "那次", "当时",
    "之前说的", "刚才说的", "后来呢", "后来怎么样", "查一下我",
)
# 纯寒暄 / 短应答：不需要翻旧账。
_SMALLTALK = re.compile(
    r"^(?:嗨|hi|hello|早|早上好|中午好|晚上好|晚安|好|嗯+|哦+|啊+|"
    r"哈哈+|好的|收到|ok|okay|在吗|在么|谢了|谢谢|拜拜|行|可以|"
    r"没事|罢了|算了|👌|👋)[\s!！.。~…,，?？]*$",
    re.I,
)
_COMMAND = re.compile(r"^[/／]")


def decide(user_text: str, *, refers_previous: bool = False,
           has_images: bool = False) -> tuple[bool, str]:
    """返回 `(要不要检索, 原因)`；原因要能直接写进影子日志。

    规则按「最确定」到「最保守」排序，命中即返回。默认 True 是刻意留的兜底：
    影子阶段只用它的分布判断「有多少轮本来不需要检索」，不据此改回复。
    """
    t = (user_text or "").strip()
    if not t:
        return (True, "empty-or-image") if has_images else (False, "empty")
    if _COMMAND.match(t):
        return False, "command"
    if any(w in t for w in _EXPLICIT):
        return True, "explicit-recall"
    if refers_previous:
        return True, "refers-previous"
    if _SMALLTALK.match(t):
        return False, "smalltalk"
    return True, "default"
