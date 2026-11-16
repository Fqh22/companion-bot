#!/usr/bin/env python3
"""[infra] 只读 MCP 客户端自检：协议、超时、白名单、注册、外部数据标记。

不起真实 MCP server：用一段 python 脚本假装，按 newline-delimited JSON-RPC 回话。
真实 server 的行为（npx / 文件系统）不在这里验，验的是「我们这侧的三条判据」。
"""
from __future__ import annotations

import asyncio
import sys

from harness import bootstrap, check, report, temp_root

bootstrap()

from app_core import mcp  # noqa: E402

SERVER = r'''
import json, sys
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    m = msg.get("method")
    if m == "initialize":
        out = {"protocolVersion": "2024-11-05", "capabilities": {}}
    elif m == "tools/list":
        out = {"tools": [{"name": "read_note", "description": "读笔记"}]}
    elif m == "tools/call":
        args = (msg.get("params") or {}).get("arguments") or {}
        out = {"content": [{"type": "text", "text": "笔记内容:" + str(args.get("q", ""))}]}
    else:
        continue
    print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": out}), flush=True)
'''

SLOW = "import time\ntime.sleep(30)\n"


def cfg_for(root) -> dict:
    srv = root / "srv.py"
    srv.write_text(SERVER, encoding="utf-8")
    return {"enabled": True, "servers": [{
        "name": "fake", "command": sys.executable, "args": [str(srv)],
        "tools": [{"name": "read_note", "description": "读笔记",
                   "parameters": {"type": "object",
                                  "properties": {"q": {"type": "string"}}}}],
    }]}


def main() -> int:
    root = temp_root("mcp-test-")
    cfg = cfg_for(root)

    async def run() -> None:
        spec = cfg["servers"][0]
        c = mcp.client_for(spec, timeout=5)
        res = await c.initialize()
        check("initialize 拿到协议版本", "protocolVersion" in res, str(res))
        tools = await c.list_tools()
        check("list_tools 拿回工具名", [t.get("name") for t in tools] == ["read_note"],
              str(tools))
        txt = await c.call_tool("read_note", {"q": "天气"})
        check("返回带外部数据标记", txt.startswith("[外部数据"), txt[:40])
        check("返回内容透传", "笔记内容:天气" in txt, txt[:60])
        await c.close()

        slow = root / "slow.py"
        slow.write_text(SLOW, encoding="utf-8")
        c2 = mcp.Client("slow", sys.executable, [str(slow)], timeout=0.8)
        try:
            await c2.initialize()
            check("超时抛 McpError", False, "没有抛")
        except mcp.McpError as e:
            check("超时抛 McpError", "超时" in str(e), str(e))
        await c2.close()

        try:
            await mcp.call("mcp__fake__write_note", {}, cfg)
            check("非白名单 tool 被拒", False, "没有抛")
        except mcp.McpError as e:
            check("非白名单 tool 被拒", "白名单" in str(e), str(e))
        try:
            await mcp.call("mcp__nope__read_note", {}, cfg)
            check("非白名单 server 被拒", False, "没有抛")
        except mcp.McpError as e:
            check("非白名单 server 被拒", "白名单" in str(e), str(e))

        from app_core import tools as tools_mod
        mcp.set_config(cfg)          # execute 里读配置，测试要把它钉住
        names = mcp.register(tools_mod, cfg)
        check("注册出 mcp__ 前缀的工具", "mcp__fake__read_note" in names, str(names))
        check("注册是幂等的",
              mcp.register(tools_mod, cfg) == [], "重复注册又加了一遍")
        check("注册的工具在 TOOLS 里",
              any(t.get("function", {}).get("name") == "mcp__fake__read_note"
                  for t in tools_mod.TOOLS))
        out = await tools_mod.execute(None, "mcp__fake__read_note", {"q": "x"})
        check("execute 走 MCP 分支", "笔记内容:x" in out, out[:60])
        out2 = await tools_mod.execute(None, "mcp__fake__nope", {})
        check("未知 MCP 工具降级成一句话", "暂时不可用" in out2, out2[:60])
        mcp.set_config(None)
        await mcp.close_all()

    asyncio.run(run())
    return report()


if __name__ == "__main__":
    raise SystemExit(main())
