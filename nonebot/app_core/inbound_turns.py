"""[orchestration] 私聊入站分批，每个通道会话只运行一个处理任务。"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable, Hashable
from dataclasses import dataclass, field
from typing import Generic, TypeVar

from app_core.turn import Turn

log = logging.getLogger("app_core")
Key = TypeVar("Key", bound=Hashable)


@dataclass
class _Batch:
    first: float
    last: float
    parts: list[Turn]
    has_text: bool
    has_file: bool
    voice: bool
    reply_to: str | None

    def combine(self) -> Turn:
        result = Turn()
        for part in sorted(self.parts, key=lambda t: t.meta["received_at"]):
            result.merge(part)
        result.meta["received_at"] = self.first
        result.meta["ready_at"] = max(t.meta["ready_at"] for t in self.parts)
        result.meta["input_count"] = len(self.parts)
        return result


@dataclass
class _Session:
    batches: deque[_Batch] = field(default_factory=deque)
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task | None = None


class InboundTurns(Generic[Key]):
    def __init__(self, handler: Callable[[Key, Turn], Awaitable[None]], *,
                 text_gap: float = 2.0, media_gap: float = 1.5) -> None:
        self.handler = handler
        self.text_gap = max(0.0, min(10.0, text_gap))
        self.media_gap = max(0.0, min(10.0, media_gap))
        self._sessions: dict[Key, _Session] = {}

    def _deadline(self, batch: _Batch) -> float:
        gap = (0.0 if batch.voice else self.text_gap if batch.has_text else
               max(self.media_gap, self.text_gap))
        return min(batch.last + gap, batch.first + max(4.0, gap))

    def submit(self, key: Key, part: Turn) -> asyncio.Task | None:
        if part.empty():
            return None
        now = time.monotonic()
        received = part.meta.setdefault("received_at", now)
        part.meta.setdefault("ready_at", now)
        reply_to = part.meta.get("reply_to")
        voice = bool(part.meta.get("voice"))
        session = self._sessions.get(key)
        if session is None:
            session = self._sessions[key] = _Session()
        batch = session.batches[-1] if session.batches else None
        if (batch is not None and not (voice or batch.voice)
                and batch.first - self.text_gap <= received < self._deadline(batch)
                and not (part.file and batch.has_file)
                and not (reply_to and batch.reply_to and reply_to != batch.reply_to)):
            batch.parts.append(part)
            batch.first = min(batch.first, received)
            batch.last = max(batch.last, received)
            batch.has_text |= bool(part.texts)
            batch.has_file |= bool(part.file)
            batch.reply_to = reply_to or batch.reply_to
        else:
            session.batches.append(_Batch(received, received, [part], bool(part.texts),
                                          bool(part.file), voice, reply_to))
        session.changed.set()
        if session.task is None:
            session.task = asyncio.create_task(self._drain(key, session))
        return session.task

    async def _drain(self, key: Key, session: _Session) -> None:
        try:
            while session.batches:
                wait = self._deadline(session.batches[0]) - time.monotonic()
                if wait > 0 and len(session.batches) == 1:
                    session.changed.clear()
                    try:
                        await asyncio.wait_for(session.changed.wait(), timeout=wait)
                    except asyncio.TimeoutError:
                        pass
                    continue
                turn = session.batches.popleft().combine()
                try:
                    await self.handler(key, turn)
                except Exception:
                    log.exception("私聊批次处理失败")
        finally:
            self._sessions.pop(key, None)
