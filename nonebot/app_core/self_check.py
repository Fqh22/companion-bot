"""[domain] 回话自检标签的解析（docs/12）。

模型在正文前写一行：
    [自检] 主=接住 次=判断 置信=高 | 依据=有 来源=记忆

这里只做两件事：剥掉这一行、把标签留下。解析失败一律原样返回——
标签是观测手段，不能让它影响回复本身。
"""
from __future__ import annotations

import re

# 标签可能以换行结尾，也可能直接跟正文（中间两个空格、甚至没有空格）。
# 「来源=」的值只有几种，用白名单收尾——用 \S+ 会把正文一起吞掉
# （实测：来源=记忆我这边收到的还是文字）。
_SRC = r"(?:记忆|搜索|无|记忆\+搜索|两者)"
_RE_FULL = re.compile(
    r"^\s*\[自检\]\s*(.+?来源\s*=\s*" + _SRC + r"(?:[（(][^）)]{0,30}[）)])?)\s*",
    re.S)
_RE_LINE = re.compile(r"^\s*\[自检\]\s*(.+?)\s*\n", re.S)


def parse(text: str) -> tuple[str, str]:
    """返回 (标签, 正文)。没有标签时标签为空串、正文原样。"""
    t = text or ""
    for rx in (_RE_FULL, _RE_LINE):
        m = rx.match(t)
        if m:
            return m.group(1).strip(), t[m.end():]
    return "", t


def looks_like_tag(text: str) -> bool:
    """流式判断用：开头像自检标签（可能还没写完）。"""
    t = (text or "").lstrip()
    return t.startswith("[自检]")


# 模型偶尔把「怎么组织这条回复」的内心独白当正文发出来。2026-11-12 实测：
# 「自检那行先按要求写？系统要求格式——需写一行自检。但注意「标签不会发给用户」。
#  我照写。正文：哪个想法，联合那套还是连 Claude 也不碰了短一点。…」
# 这类文本没有任何信息量，还会进历史。只认强特征、且至少命中两条，
# 避免误杀真正在讨论「正文」的回复（比如用户让它写文章）。
_META_MARKERS = ("自检那行", "系统要求格式", "需写一行自检", "标签不会发给用户",
                 "我照写", "正文：", "内心独白", "让我想想怎么")


def is_meta(text: str) -> bool:
    """整段是不是「怎么回复」的独白，而不是回复本身。"""
    s = (text or "").strip()
    if not s or len(s) > 600:      # 长文里的「正文：」多半是真的在写东西
        return False
    return sum(m in s for m in _META_MARKERS) >= 2
