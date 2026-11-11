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
