"""冻结桩：时钟与日期源。

独立成文件的原因：
1. 基线 manifest 要能对「冻结逻辑本身」做 hash（否则 S1a 期间有人放宽冻结，基线没动却失效）；
2. 自检要能篡改它并断言 sha 变（证明冻结真的在起作用）。

不冻 monotonic：asyncio 的超时依赖它，冻死会把 asyncio.wait 卡住。

**时区也必须冻**：localtime / datetime.now 返回的是本地时间。
基线在 Asia/Shanghai 生成，GitHub Actions 的 runner 是 UTC——
不冻时区的话，prompt 里「现在：2026-11-15 20:00 周日（晚上）」会变成「12:00 中午」，
36 条 sha 全红，而本地/云端（都 +08）永远复现不了。
"""
from __future__ import annotations

import datetime as _dtmod
import time as _time

FIXED_TS = 1794744000.0                      # 2026-11-15 12:00 UTC = 20:00 +08
_CST = _dtmod.timezone(_dtmod.timedelta(hours=8))

_real_time = _time.time
_real_gmtime = _time.gmtime
_real_strftime = _time.strftime
_real_datetime = _dtmod.datetime


def _frozen_struct(ts: float | None = None):
    """固定时点的 +08 struct_time，与宿主时区无关。"""
    return _real_gmtime((FIXED_TS if ts is None else ts) + 8 * 3600)


class _FrozenDatetime(_real_datetime):
    """只替换 now()。

    prompt.py:53 是**函数内** from datetime import datetime，
    所以 patch datetime.datetime 能生效；返回 naive 的 +08 时间，与基线一致。
    """

    @classmethod
    def now(cls, tz=None):
        if tz is not None:
            return _real_datetime.fromtimestamp(FIXED_TS, tz=tz)
        return _real_datetime.fromtimestamp(FIXED_TS, tz=_CST).replace(tzinfo=None)


def install(ts: float | None = None) -> None:
    global FIXED_TS
    if ts is not None:
        FIXED_TS = ts
    _time.time = lambda: FIXED_TS
    _time.localtime = lambda secs=None: _frozen_struct(secs)
    _time.strftime = lambda fmt, t=None: _real_strftime(
        fmt, _frozen_struct() if t is None else t)
    _dtmod.datetime = _FrozenDatetime


def set_ts(ts: float) -> None:
    global FIXED_TS
    FIXED_TS = ts


def sources() -> list[str]:
    return ["time.time", "time.localtime(+08)", "time.strftime",
            "datetime.datetime.now(+08)", "mood 固定值注入"]
