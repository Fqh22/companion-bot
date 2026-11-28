"""[orchestration] 私聊入站分批，每个通道会话只运行一个处理任务。"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable, Hashable
from dataclasses import dataclass, field
from typing import Generic, TypeVar

from app_core.turn import Turn, expects_more

log = logging.getLogger("app_core")
Key = TypeVar("Key", bound=Hashable)
ImageLoad = Callable[[], Awaitable[str | None]]
VoiceLoad = Callable[[], Awaitable[str]]


@dataclass
class _Download:
    path: str | None
    ready_at: float


@dataclass
class _Batch:
    first: float
    last: float
    parts: list[Turn]
    has_text: bool
    has_file: bool
    voice: bool
    reply_to: str | None
    downloads: list[asyncio.Task[_Download]] = field(default_factory=list)
    transcripts: dict[int, asyncio.Task[_Download]] = field(default_factory=dict)
    image_sources: dict[str, list[asyncio.Task[_Download]]] = field(default_factory=dict)

    async def combine(self) -> Turn:
        result = Turn()
        ready_at = max(t.meta["ready_at"] for t in self.parts)
        voice_unavailable = False
        for part in sorted(self.parts, key=lambda t: t.meta["received_at"]):
            if task := self.transcripts.get(id(part)):
                transcript = await task
                ready_at = max(ready_at, transcript.ready_at)
                if transcript.path:
                    part.texts.append(transcript.path)
                else:
                    voice_unavailable = True
                    part.meta["voice"] = False
            result.merge(part)
        result.meta["received_at"] = self.first
        result.meta["voice_unavailable"] = voice_unavailable
        if self.downloads:
            downloads = await asyncio.gather(*(asyncio.shield(task) for task in self.downloads))
            for download in downloads:
                if download.path:
                    result.images.append(download.path)
                ready_at = max(ready_at, download.ready_at)
            result.images = list(dict.fromkeys(result.images))
            result.meta["image_unavailable"] = any(item.path is None for item in downloads)
            result.meta["image_sources"] = {
                event_id: [item.path for task in tasks if (item := task.result()).path]
                for event_id, tasks in self.image_sources.items()
            }
        result.meta["ready_at"] = ready_at
        result.meta["input_count"] = len(self.parts)
        return result


@dataclass
class _Session:
    batches: deque[_Batch] = field(default_factory=deque)
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task | None = None


class InboundTurns(Generic[Key]):
    QUICK_GAP = 0.9
    REFERENCE_SECONDS = 15 * 60
    REFERENCE_LIMIT = 256

    def __init__(self, handler: Callable[[Key, Turn], Awaitable[None]], *,
                 text_gap: float = 2.0, media_gap: float = 1.5,
                 media_only_gap: float | None = None, image_timeout: float = 8.0) -> None:
        self.handler = handler
        self.text_gap = max(0.0, min(10.0, text_gap))
        self.media_gap = max(0.0, min(10.0, media_gap))
        self.media_only_gap = (max(0.0, min(10.0, media_only_gap))
                               if media_only_gap is not None
                               else max(self.media_gap, self.text_gap))
        self.image_timeout = image_timeout
        self._sessions: dict[Key, _Session] = {}
        self._image_slots = asyncio.Semaphore(4)
        self._image_refs: OrderedDict[tuple[Key, str], tuple[float, list[asyncio.Task[_Download]]]] = OrderedDict()

    def _deadline(self, batch: _Batch) -> float:
        if batch.downloads and not batch.has_text and not batch.voice:
            gap = max(self.media_only_gap, self.media_gap, self.text_gap)
        elif batch.voice or not batch.has_text:
            gap = max(self.media_gap, self.text_gap)
        elif len(batch.parts) > 1 or expects_more(batch.parts[0].texts[-1]):
            gap = self.text_gap
        else:
            gap = min(self.QUICK_GAP, self.text_gap)
        cap = max(4.0, gap + 1.5 if gap > 3.0 else gap)
        return min(batch.last + gap, batch.first + cap)

    async def _download(self, load_images: ImageLoad) -> _Download:
        async def load() -> str | None:
            async with self._image_slots:
                return await load_images()

        try:
            path = await asyncio.wait_for(load(), timeout=self.image_timeout)
        except Exception as exc:
            log.warning("私聊图片读取失败（%s）", type(exc).__name__)
            path = None
        return _Download(path, time.monotonic())

    async def _transcribe(self, load: VoiceLoad) -> _Download:
        try:
            text = await asyncio.wait_for(load(), timeout=18.0)
        except Exception as exc:
            log.warning("私聊语音读取失败（%s）", type(exc).__name__)
            text = ""
        return _Download(text or None, time.monotonic())

    def submit(self, key: Key, part: Turn, *, image_loaders: tuple[ImageLoad, ...] = (),
               voice_loader: VoiceLoad | None = None) -> asyncio.Task | None:
        if part.empty() and not image_loaders and voice_loader is None:
            return None
        now = time.monotonic()
        received = part.meta.setdefault("received_at", now)
        part.meta.setdefault("ready_at", now)
        reply_to = part.meta.get("reply_to")
        voice = bool(part.meta.get("voice") or voice_loader is not None)
        if voice:
            part.meta["voice"] = True
        while self._image_refs and next(iter(self._image_refs.values()))[0] <= now:
            self._image_refs.popitem(last=False)
        downloads = [asyncio.create_task(self._download(load)) for load in image_loaders]
        own_downloads = downloads.copy()
        if downloads:
            if event_id := part.meta.get("event_id"):
                index = (key, str(event_id))
                self._image_refs[index] = (now + self.REFERENCE_SECONDS, downloads.copy())
                self._image_refs.move_to_end(index)
                if len(self._image_refs) > self.REFERENCE_LIMIT:
                    self._image_refs.popitem(last=False)
        if reply_to and (ref := self._image_refs.get((key, str(reply_to)))):
            downloads.extend(ref[1])
        session = self._sessions.get(key)
        if session is None:
            session = self._sessions[key] = _Session()
        batch = session.batches[-1] if session.batches else None
        if (batch is not None and not (voice and batch.voice)
                and batch.first - self.text_gap <= received < self._deadline(batch)
                and not (part.file and batch.has_file)
                and not (reply_to and batch.reply_to and reply_to != batch.reply_to)):
            batch.parts.append(part)
            batch.first = min(batch.first, received)
            batch.last = max(batch.last, received)
            batch.has_text |= bool(part.texts)
            batch.has_file |= bool(part.file)
            batch.voice |= voice
            batch.reply_to = reply_to or batch.reply_to
        else:
            batch = _Batch(received, received, [part], bool(part.texts), bool(part.file), voice, reply_to)
            session.batches.append(batch)
        batch.downloads = list(dict.fromkeys([*batch.downloads, *downloads]))
        if own_downloads and part.meta.get("event_id"):
            batch.image_sources[str(part.meta["event_id"])] = own_downloads
        if voice_loader is not None:
            batch.transcripts[id(part)] = asyncio.create_task(self._transcribe(voice_loader))
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
                batch = session.batches.popleft()
                try:
                    await self.handler(key, await batch.combine())
                except Exception:
                    log.exception("私聊批次处理失败")
        finally:
            self._sessions.pop(key, None)
