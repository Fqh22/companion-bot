"""入口：装配所有组件，跑主循环。

用法：
    python -m agentd.main           常驻
    python -m agentd.main --once    跑完当前队列就退出（测试用）
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

import httpx

from . import channel, config
from .config import Config
from .context import Context
from .llm import LLM
from .loop import Runner
from .planner import Planner
from .sandbox import Sandbox
from .store import Store
from .tools import Tools, tools_doc

log = logging.getLogger("agentd")


def build(cfg: Config | None = None):
    cfg = cfg or Config.load()
    config.ensure_dirs()
    store = Store()
    llm = LLM(cfg, store)
    sandbox = Sandbox(cfg, config.WORK)
    tools = Tools(cfg, sandbox, config.WORK)
    ctx = Context(cfg, store, llm, tools_doc(cfg.memory_enabled), work=config.WORK)
    planner = Planner(cfg, store, llm)
    runner = Runner(cfg, store, llm, sandbox, tools, ctx, planner, config.WORK)
    return cfg, store, runner


async def _probe_memory(cfg: Config) -> None:
    """探测记忆库；连不上就把工具表里的两个记忆工具摘掉（它们本来也用不了）。"""
    try:
        async with httpx.AsyncClient(timeout=3) as c:
            r = await c.get(f"{cfg.rag_api}/health")
        cfg.memory_enabled = r.status_code == 200
    except Exception:  # noqa: BLE001
        cfg.memory_enabled = False
    log.info("记忆库 %s", "可用" if cfg.memory_enabled else "不可用（已从工具表摘掉）")


async def _run(once: bool) -> None:
    cfg = Config.load()
    await _probe_memory(cfg)
    cfg, store, runner = build(cfg)
    if not once:
        channel.start(store, cfg)
    log.info("agentd 启动，端口 %s，模型 %s", cfg.port, cfg.model)
    if once:
        task = store.running_task() or store.next_pending_task()
        while task:
            await runner.run_task(task)
            task = store.running_task() or store.next_pending_task()
        return
    await runner.run_forever()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--log", default="INFO")
    a = ap.parse_args()
    logging.basicConfig(
        level=getattr(logging, a.log.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s | %(message)s")
    try:
        asyncio.run(_run(a.once))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
