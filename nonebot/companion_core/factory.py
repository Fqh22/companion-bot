"""应用组装、初始化和关闭。通道由宿主接入。"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from functools import cache
from pathlib import Path

from companion_core.config import EngineConfig

log = logging.getLogger("qqbot.application")
Hook = Callable[[], Awaitable[None]]


class Application:
    def __init__(self, engine):
        self.engine = engine
        self.core = engine.core
        self._lock = asyncio.Lock()
        self._started = False
        self._catch_up_task: asyncio.Task | None = None
        self._background: list[tuple[Hook, Hook]] = []

    def register_background(self, start: Hook, stop: Hook) -> None:
        """注册宿主提供的后台服务，先初始化内核，再启动服务。"""
        if self._started:
            raise RuntimeError("后台服务须在应用启动前注册")
        service = (start, stop)
        if service not in self._background:
            self._background.append(service)

    async def start(self, *, catch_up: bool = False) -> None:
        async with self._lock:
            if self._started:
                return
            await self.core.init_db()
            self._started = True
            for issue in self.core.config_issues():
                log.error("[config] %s", issue)
            log.info("chat core ready（%s）", self.core.config_status())
            self._record_latency_settings()
            if catch_up:
                self._catch_up_task = asyncio.create_task(self._catch_up())
            for start, _ in self._background:
                try:
                    await start()
                except Exception:
                    log.exception("后台服务启动失败：%s", start.__module__)

    def _record_latency_settings(self) -> None:
        if not hasattr(self.core, "reply_deadline"):
            return
        from app_core import config, round_timing

        try:
            version = (Path(__file__).resolve().parents[1] / ".deployed_sha").read_text(
                encoding="utf-8").strip()
        except OSError:
            version = "local"
        settings = config.load()
        llm = settings.get("llm") or {}
        parallel = llm.get("prepare_parallel") or {}
        client = getattr(self.core, "llm", None)
        route = getattr(client, "route", None)
        summary_route = getattr(client, "summary_route", None) or route
        round_timing.bind_release(version, {
            "chat_protocol": getattr(route, "protocol", "unknown"),
            "summary_protocol": getattr(summary_route, "protocol", "unknown"),
            "chat_model": getattr(client, "model", ""),
            "summary_model": getattr(client, "summary_model", ""),
            "reply_deadline_seconds": self.core.reply_deadline,
            "first_token_budget_seconds": self.core.first_token_budget,
            "prepare_parallel": {key: bool(parallel.get(key)) for key in ("search", "weather")},
            "force_search": bool((settings.get("chat") or {}).get("force_search", True)),
        })

    async def _catch_up(self) -> None:
        try:
            result = await self.core.catch_up_summary()
            if result.get("ran"):
                log.info("[summary] 启动补偿：补了 %s 轮未总结的对话", result.get("pending"))
            elif result.get("reason") == "no-admin":
                log.warning("[summary] 启动补偿跳过：admin.user_id 未配置")
        except Exception as exc:
            log.warning("[summary] 启动补偿失败：%s", exc)

    async def stop(self) -> None:
        async with self._lock:
            task = self._catch_up_task
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            self._catch_up_task = None
            if self._started:
                from app_core import embed_provider, mcp

                for _, stop in reversed(self._background):
                    try:
                        await stop()
                    except Exception:
                        log.exception("后台服务停止失败：%s", stop.__module__)
                try:
                    await mcp.close_all()
                finally:
                    await embed_provider.close()
            self._started = False


def create_engine(config: EngineConfig | None = None, core=None):
    if config is not None:
        config.apply()
    from app_core.engine import Engine
    if core is None:
        from app_core.chat import ChatCore
        core = ChatCore()
    return Engine(core)


def create_application(config: EngineConfig | None = None, core=None) -> Application:
    return Application(create_engine(config, core))


@cache
def shared_application() -> Application:
    """NoneBot 宿主的进程级实例。各通道接同一个应用。"""
    return create_application()
