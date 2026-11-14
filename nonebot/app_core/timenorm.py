"""把「下周三 / 明天 / 月底」归一化成绝对日期。

为什么在写入侧做：prompt 里要求模型换算，实测它能开始算但会算错一天
（今天 11-14 周六，下周三应为 11-19，它答 11-18 并说「周三」）。
Python 算日期不会错，所以把这一步从模型手里拿走。

保守原则：只处理明确的表达，歧义的原样保留——
「周五」可能是本周也可能是下周，不猜。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

_WK = "一二三四五六日"


def _idx(ch: str) -> int:
    return _WK.index(ch) if ch in _WK else -1


def normalize(text: str, now: datetime | None = None) -> str:
    """返回替换后的文本；识别不了的原样返回。"""
    if not text:
        return text
    t = now or datetime.now()
    out = text

    for word, days in (("大后天", 3), ("后天", 2), ("明天", 1)):
        if word in out:
            d = t + timedelta(days=days)
            out = out.replace(word, f"{d:%m-%d}（周{_WK[d.weekday()]}）")

    def _next_week(m):
        i = _idx(m.group(1))
        if i < 0:
            return m.group(0)
        monday = t - timedelta(days=t.weekday())
        d = monday + timedelta(days=7 + i)
        return f"{d:%m-%d}（周{_WK[i]}）"

    out = re.sub(r"下周([一二三四五六日])", _next_week, out)

    if "月底" in out:
        nxt = (t.replace(day=1) + timedelta(days=32)).replace(day=1)
        d = nxt - timedelta(days=1)
        out = out.replace("月底", f"{d:%m-%d}")

    return out
