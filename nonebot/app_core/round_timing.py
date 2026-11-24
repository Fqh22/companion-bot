"""[orchestration] 收件到投递的分段计时。日志只含阶段和时长。"""
from __future__ import annotations

import json
import logging
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from uuid import uuid4

log = logging.getLogger("qqbot.latency")
_release = "local"


def bind_release(version: str, settings: dict) -> None:
    global _release
    _release = version
    log.info("[latency-settings] %s", json.dumps({"release": version, **settings}))


@dataclass
class TurnTiming:
    platform: str
    received: float = field(default_factory=time.monotonic)
    rid: str = field(default_factory=lambda: uuid4().hex[:10])
    marks: dict[str, float] = field(default_factory=dict)
    reply_form: str = "text"
    input_count: int = 1
    checking: int = 0
    status: str = "ok"

    def mark(self, phase: str) -> None:
        self.marks.setdefault(phase, time.monotonic())

    def emit(self) -> None:
        self.mark("finish")
        if self.status == "ok" and "first_sent" not in self.marks:
            self.status = "no_delivery"
        fields = {f"{key}_ms": round(max(0, end - self.received) * 1000)
                  for key, end in self.marks.items()}
        log.info("[latency] %s", json.dumps({
            "rid": self.rid, "platform": self.platform, "form": self.reply_form,
            "status": self.status, "release": _release, "inputs": self.input_count, **fields,
        }, ensure_ascii=False))


current: ContextVar[TurnTiming | None] = ContextVar("current_turn_timing", default=None)
