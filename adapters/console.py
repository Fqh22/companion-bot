"""ConsoleChannel：把引擎接到终端，验证“脱离 QQ 也能跑”。"""
from __future__ import annotations

import asyncio
import sys

from app_core.engine import Engine, InboundMessage, OutboundMessage


class ConsoleChannel:
    def __init__(self, out=None) -> None:
        self.out = out or sys.stdout

    async def send(self, out: OutboundMessage) -> None:
        for chunk in out.chunks:
            self.out.write(chunk + "\n")
        for path in out.images:
            self.out.write(f"[图片] {path}\n")
        self.out.flush()

    async def run(self, engine: Engine, session: str = "private:console") -> None:
        self.out.write("console 模式；输入 /quit 退出。\n")
        self.out.flush()
        while True:
            line = await asyncio.to_thread(input, "你: ")
            text = (line or "").strip()
            if text in ("/quit", "/exit", "exit"):
                break
            if not text:
                continue
            out = await engine.handle(InboundMessage(session=session, text=text))
            await self.send(out)
