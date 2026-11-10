"""ConsoleChannel：把引擎接到终端，验证「脱离 QQ 也能跑」。

它是 app_core.runner.Channel 的参考实现：只做三件 IO 原语，
会话循环（typing / 流式 / 45 秒 / 分段节奏）全在 app_core.runner 里。
新通道照着这个文件写，不需要 NoneBot，也不需要 QQ。
"""
from __future__ import annotations

import asyncio
import sys

from app_core.commands import CommandContext, default_registry
from app_core.delivery import Capability
from app_core.engine import Engine, InboundMessage, OutboundMessage
from app_core.runner import TurnRunner


class ConsoleChannel:
    def __init__(self, out=None, caps: Capability | None = None) -> None:
        self.out = out or sys.stdout
        self.caps = caps or Capability(
            name="console", max_text_chars=2000, max_parts=4, supports_images=True)

    async def send_text(self, session: str, text: str) -> bool:
        self.out.write(text + "\n")
        self.out.flush()
        return True

    async def send_image(self, session: str, path: str) -> bool:
        self.out.write(f"[图片] {path}\n")
        self.out.flush()
        return True

    async def typing(self, session: str, on: bool) -> None:
        # 终端没有「正在输入」；真实通道在这里实现自己的状态提示
        return None

    async def send(self, out: OutboundMessage) -> None:
        """整包发送（测试和一次性调用用；run() 走 runner 的流式）。"""
        for chunk in out.chunks:
            await self.send_text("", chunk)
        for path in out.images:
            await self.send_image("", path)

    async def run(self, engine: Engine, session: str = "private:console") -> None:
        runner = TurnRunner(engine, self, caps=self.caps)
        core = getattr(engine, "core", None)
        registry = default_registry(core) if core is not None else None
        self.out.write("console 模式；/help 看命令，/quit 退出。\n")
        self.out.flush()
        while True:
            try:
                line = await asyncio.to_thread(input, "你: ")
            except EOFError:      # 管道/重定向输入（演练、脚本）读到结尾
                break
            text = (line or "").strip()
            if text in ("/quit", "/exit", "exit"):
                break
            if not text:
                continue
            if registry is not None and text.startswith("/"):
                out = await registry.dispatch(
                    text, CommandContext(session=session, is_owner=True))
                if out is not None:
                    self.out.write(out + "\n")
                    self.out.flush()
                    continue
            await runner.run(InboundMessage(session=session, text=text))
