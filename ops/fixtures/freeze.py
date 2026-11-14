"""冻结桩：时钟与日期源。

独立成文件的原因：
1. 基线 manifest 要能对「冻结逻辑本身」做 hash（否则 S1a 期间有人放宽冻结，基线没动却失效）；
2. 自检要能篡改它并断言 sha 变（证明冻结真的在起作用）。

不冻 monotonic：asyncio 的超时依赖它，冻死会把 asyncio.wait 卡住。
"""
from __future__ import annotations

import datetime as _dtmod
import time as _time

FIXED_TS = 1794744000.0                      # 2026-11-15 12:00 +08

_real_time = _time.time
_real_localtime = _time.localtime
_real_strftime = _time.strftime
_real_datetime = _dtmod.datetime


class _FrozenDatetime(_real_datetime):
    """只替换 now()。

    prompt.py:53 是**函数内** from datetime import datetime，
    所以 patch datetime.datetime 能生效；顶层已 import 的模块持有旧引用，
    不受影响（它们也不在 prompt 关键路径上）。
    """

    @classmethod
    def now(cls, tz=None):
        return _real_datetime.fromtimestamp(FIXED_TS, tz)


def install(ts: float | None = None) -> None:
    global FIXED_TS
    if ts is not None:
        FIXED_TS = ts
    _time.time = lambda: FIXED_TS
    _time.localtime = lambda secs=None: _real_localtime(FIXED_TS if secs is None else secs)
    _time.strftime = lambda fmt, t=None: _real_strftime(
        fmt, _real_localtime(FIXED_TS) if t is None else t)
    _dtmod.datetime = _FrozenDatetime


def set_ts(ts: float) -> None:
    global FIXED_TS
    FIXED_TS = ts


def sources() -> list[str]:
    return ["time.time", "time.localtime", "time.strftime",
            "datetime.datetime.now", "mood 固定值注入"]
