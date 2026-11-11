"""[domain] 无来源的具体断言检测：只报告，不改回复。

为什么需要：模型会编机构名和数据（2026-11-11 实测：「科锐国际 9 月数据」「789%」
不在记忆库也不在搜索结果里）。prompt 只能降低概率，不能杜绝——生成时它并不自检。
这里做**检测**，先把误报率摸清楚，再决定要不要拦截。

为什么只报告：直接删或重写会误伤（「一年 365 天」也含数字），
在没有数据之前拦截，比编造更糟。
"""
from __future__ import annotations

import re

# 只抓「可能被当成事实」的具体断言，不抓普通数字（3 天、2 条）。
_PATTERNS = (
    re.compile(r"\d+(?:\.\d+)?%"),                       # 百分比
    re.compile(r"\d{4}\s*年\s*\d{1,2}\s*月"),            # 年月
    re.compile(r"《[^》]{2,40}》"),                          # 书名/报告名
    re.compile(r"[\u4e00-\u9fa5A-Za-z]{2,12}(?:报告|数据|研究|调查|白皮书)"),
)


def claims(text: str) -> list[str]:
    """抽出文本里的具体断言（去重、保序）。"""
    out: list[str] = []
    for pat in _PATTERNS:
        for m in pat.findall(text or ""):
            if m not in out:
                out.append(m)
    return out


def unsourced(text: str, allowed: str) -> list[str]:
    """哪些断言在 allowed（本轮注入内容 + 搜索结果）里找不到。"""
    allowed = allowed or ""
    return [c for c in claims(text) if c not in allowed]
