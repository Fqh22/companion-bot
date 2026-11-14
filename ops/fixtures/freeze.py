"""冻结桩：时钟、日期、时区。

为什么逐项而不是「跑一遍看 sha 对不对」：sha 没变可能只是那 36 条没走到这个源，
S2 一旦走进就漂。这里列全表，selftest 逐项断言返回值固定。

不冻 monotonic：asyncio 的超时依赖它，冻死会把 asyncio.wait 卡住。

时区必须显式冻：基线在 Asia/Shanghai 生成，GitHub Actions 是 UTC。
不冻的话「现在：20:00 周日（晚上）」会变「12:00 中午」，36 条 sha 全红，
而本地/云端（都 +08）永远复现不了——2026-11-14 就是这么红了 4 个 commit。
"""
from __future__ import annotations

import datetime as _dtmod
import time as _time

FIXED_TS = 1794744000.0                      # 2026-11-15 12:00 UTC = 20:00 +08
FIXED_DATE = (2026, 11, 15)
FIXED_STRUCT = (2026, 11, 15, 20, 0, 0, 6, 319, 0)   # 周日 20:00，tm_isdst=0
_CST = _dtmod.timezone(_dtmod.timedelta(hours=8))

_real_time = _time.time
_real_gmtime = _time.gmtime
_real_strftime = _time.strftime
_real_datetime = _dtmod.datetime
_real_date = _dtmod.date


def _frozen_struct(ts: float | None = None):
    """固定时点的 +08 struct_time，与宿主时区无关。

    传 ts 时用 gmtime(ts + 8h)：确定性只依赖入参，不依赖 TZ。
    tm_isdst 由 gmtime 给 0，与 +08（无夏令时）一致；换到有 DST 的地区要改这里。
    """
    if ts is None:
        return _time.struct_time(FIXED_STRUCT)
    return _real_gmtime(ts + 8 * 3600)


class _FrozenDatetime(_real_datetime):
    """替换 now / utcnow / fromtimestamp。prompt.py:53 是函数内 import，patch 类才生效。"""

    @classmethod
    def now(cls, tz=None):
        if tz is not None:
            return _real_datetime.fromtimestamp(FIXED_TS, tz=tz)
        return _real_datetime.fromtimestamp(FIXED_TS, tz=_CST).replace(tzinfo=None)

    @classmethod
    def utcnow(cls):
        return _real_datetime.fromtimestamp(FIXED_TS, tz=_dtmod.timezone.utc).replace(tzinfo=None)

    @classmethod
    def fromtimestamp(cls, ts, tz=None):
        # 源码里任何 fromtimestamp(ts) 不带 tz 的调用都会落到宿主时区——
        # 这是和 localtime 同一个坑，只是入口不同。统一按 +08 解释。
        if tz is None:
            return _real_datetime.fromtimestamp(ts, tz=_CST).replace(tzinfo=None)
        return _real_datetime.fromtimestamp(ts, tz=tz)


class _FrozenDate(_real_date):
    @classmethod
    def today(cls):
        return cls(*FIXED_DATE)


def install(ts: float | None = None) -> None:
    global FIXED_TS
    if ts is not None:
        FIXED_TS = ts
    _time.time = lambda: FIXED_TS
    _time.localtime = lambda secs=None: _frozen_struct(secs)
    _time.strftime = lambda fmt, t=None: _real_strftime(
        fmt, _frozen_struct() if t is None else t)
    # 时区名与偏移：让「系统认为是哪个时区」也确定，而不是靠宿主
    _time.tzname = ("CST", "CST")
    _time.timezone = -8 * 3600
    _time.daylight = 0
    _dtmod.datetime = _FrozenDatetime
    _dtmod.date = _FrozenDate


def set_ts(ts: float) -> None:
    global FIXED_TS
    FIXED_TS = ts


# 有意不打桩的源 + 理由。不写下来，下一个人会以为「全表」覆盖完了。
UNFROZEN = {
    "time.monotonic": "asyncio 的超时依赖它，冻死会把 asyncio.wait 卡住；只允许出现在调度层",
    "time.perf_counter": "Turn.t0 的耗时基线，S2 的 prepare_ms 也要用它；不进 prompt 渲染",
}


def snapshot() -> dict:
    """当前所有时钟源的返回值——selftest 逐项断言用。"""
    import datetime as d
    return {
        "time.time": _time.time(),
        "time.localtime": tuple(_time.localtime())[:6],
        "time.strftime": _time.strftime("%Y-%m-%d %H:%M"),
        "datetime.now": d.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "datetime.utcnow": d.datetime.utcnow().strftime("%Y-%m-%d %H:%M"),
        "date.today": d.date.today().isoformat(),
        "fromtimestamp": d.datetime.fromtimestamp(0).strftime("%Y-%m-%d %H:%M"),
        "tzname": list(_time.tzname),
        "timezone": _time.timezone,
        "daylight": _time.daylight,
    }


def sources() -> list[str]:
    return list(snapshot().keys())
