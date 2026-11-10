"""agentd 的 HTTP 客户端：给自然语言任务入口复用，不 import nonebot。"""
from __future__ import annotations

import httpx

API = "http://127.0.0.1:8100"


async def create_task(goal: str, session: str, files: list[str] | None = None,
                      api: str = API) -> dict:
    body = {"goal": goal, "session": session}
    if files:
        body["files"] = list(files)
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.post(f"{api}/task", json=body)
        return r.json()
