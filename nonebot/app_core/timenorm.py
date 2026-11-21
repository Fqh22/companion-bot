"""[domain] 把「下周三 / 明天 / 昨天 / 月底」归一化成绝对日期。

为什么在写入侧做：prompt 里要求模型换算，实测它能开始算但会算错一天
（今天 11-14 周六，下周三应为 11-19，它答 11-18 并说「周三」）。
Python 算日期不会错，所以把这一步从模型手里拿走。

保守原则：只处理明确的表达，歧义的原样保留——
「周五」可能是本周也可能是下周，不猜；**不知道这句话是哪天说的，也不猜**
（原来默认拿「写库当天」当基准，批量总结跨天时会给旧消息补一个错的日期，
见 memory_store.add_document 的 anchor_ts）。

2026-11-22 两处修正：
- 输出带完整年份（`2026-11-21（周六）`）：原来只有 `11-21`，跨年后再看这句
  话无法消歧；
- 补「昨天 / 前天 / 大前天」和「今晚 / 明早 / 明晚 / 昨晚」：原来只换算纯未来
  日期，他回忆旧事时这些词会原样留在长期记忆里，过几天就没人知道是哪天。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

_WK = "一二三四五六日"

# 明确表达：以「说话那一天」为基准唯一确定。长词在前，否则「大后天」会先
# 被「后天」吃掉、只留下一个「大」。
_REL = (("大后天", 3), ("大前天", -3), ("后天", 2), ("前天", -2),
        ("明天", 1), ("昨天", -1),
        ("明早", 1, "早上"), ("明晚", 1, "晚上"),
        ("今晚", 0, "晚上"), ("昨晚", -1, "晚上"))


def _idx(ch: str) -> int:
    return _WK.index(ch) if ch in _WK else -1


def from_ts(ts: float) -> datetime:
    """消息时间戳 → 「当时」的本地时间。

    统一走这一个入口，而不是每处自己 fromtimestamp：云端和本地部署都是
    Asia/Shanghai；测试的冻结桩也从这里过（它把 fromtimestamp 固定成 +08）。
    """
    return datetime.fromtimestamp(float(ts))


def anchor(ts: float | None) -> datetime | None:
    """取「这句话是什么时候说的」；没有来源时间就返回 None（不猜）。"""
    try:
        t = float(ts or 0)
    except (TypeError, ValueError):
        return None
    return from_ts(t) if t > 0 else None


def _fmt(d: datetime) -> str:
    return f"{d:%Y-%m-%d}（周{_WK[d.weekday()]}）"


def normalize(text: str, now: datetime | None = None) -> str:
    """返回替换后的文本；识别不了、或没有基准时间时原样返回。

    now 必须是**这句话说出来的时间**，不是写库时间——批量总结的一段对话可能
    横跨几天，用写库当天当基准就是一天不差地错。
    """
    if not text or now is None:
        return text
    t = now
    out = text

    for item in _REL:
        word, days = item[0], item[1]
        period = item[2] if len(item) > 2 else ""
        if word in out:
            d = t + timedelta(days=days)
            out = out.replace(word, _fmt(d) + period)

    def _next_week(m):
        i = _idx(m.group(1))
        if i < 0:
            return m.group(0)
        monday = t - timedelta(days=t.weekday())
        d = monday + timedelta(days=7 + i)
        return _fmt(d)

    out = re.sub(r"下周([一二三四五六日])", _next_week, out)

    if "月底" in out:
        nxt = (t.replace(day=1) + timedelta(days=32)).replace(day=1)
        d = nxt - timedelta(days=1)
        out = out.replace("月底", _fmt(d))

    return out
