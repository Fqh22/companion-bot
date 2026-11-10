"""命令分发（app_core.commands）的离线测试：解析、权限、默认命令集。

不导入 nonebot：验证「新通道拿同一个注册表就能回 /help /ping」。
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

CODE = Path(os.environ.get("QQBOT_CODE_DIR", "/opt/nonebot")).resolve()
sys.path.insert(0, str(CODE))
os.environ.setdefault("QQBOT_ROOT", tempfile.mkdtemp(prefix="cmd-test-"))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

from app_core.commands import (  # noqa: E402
    CommandContext, CommandRegistry, CommandSpec, default_registry)

RESULTS: list[bool] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    RESULTS.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + name + (f" | {extra}" if extra else ""))


async def main() -> None:
    parse = CommandRegistry.parse
    check("解析命令与参数", parse("/mood 7") == ("mood", "7"), str(parse("/mood 7")))
    check("无参数", parse("/help") == ("help", ""), str(parse("/help")))
    check("非命令返回 None", parse("你好") is None)
    check("只有斜杠返回 None", parse("/") is None)
    check("大小写归一", parse("/HELP") == ("help", ""), str(parse("/HELP")))

    async def echo(ctx: CommandContext) -> str:
        return "args=" + ctx.args

    r = CommandRegistry()
    r.register(CommandSpec("echo", "重复", echo))
    check("分发到 handler",
          await r.dispatch("/echo 你好", CommandContext(session="s", is_owner=True)) == "args=你好")
    check("未知命令返回 None",
          await r.dispatch("/nope", CommandContext(session="s")) is None)
    check("非命令返回 None",
          await r.dispatch("hi", CommandContext(session="s")) is None)

    r.register(CommandSpec("secret", "只有主人", echo))
    check("非主人被拦",
          await r.dispatch("/secret", CommandContext(session="s", is_owner=False))
          == "这条命令只有主人能用。")

    class FakeCore:
        def config_issues(self):
            return []

        def config_status(self):
            return "ok"

    reg = default_registry(FakeCore())
    check("默认命令集齐全",
          set(reg.names()) >= {"ping", "help", "debug", "mood", "usage", "mem_status", "profile",
                               "memory_cleanup", "diary_write", "review_now", "weekly_now"},
          str(reg.names()))
    out = await reg.dispatch("/ping", CommandContext(session="s", is_owner=True))
    check("ping 可用", bool(out) and out.startswith("pong"), str(out)[:40])

    passed = sum(1 for x in RESULTS if x)
    print(f"\n=== {passed}/{len(RESULTS)} 通过 ===")
    sys.exit(0 if passed == len(RESULTS) else 1)


if __name__ == "__main__":
    asyncio.run(main())
