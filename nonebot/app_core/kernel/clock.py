"""时间来源：业务代码不要直接 time.time()，方便测试注入。"""
from __future__ import annotations

import time
from datetime import date, datetime
from typing import Protocol


class Clock(Protocol):
    def now(self) -> float: ...
    def today(self) -> date: ...
    def datetime(self) -> datetime: ...


class SystemClock:
    def now(self) -> float:
        return time.time()

    def today(self) -> date:
        return date.today()

    def datetime(self) -> datetime:
        return datetime.now()


class FakeClock:
    """测试用：显式推进时间，不依赖真实时钟。"""

    def __init__(self, ts: float):
        self._ts = float(ts)

    def now(self) -> float:
        return self._ts

    def today(self) -> date:
        return date.fromtimestamp(self._ts)

    def datetime(self) -> datetime:
        return datetime.fromtimestamp(self._ts)

    def advance(self, seconds: float) -> None:
        self._ts += float(seconds)
