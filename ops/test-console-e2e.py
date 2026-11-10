#!/usr/bin/env python3
"""M2.1：用真实 ChatCore + fake LLM 跑通 console 完整链路。"""
from __future__ import annotations

import asyncio
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(tempfile.mkdtemp(prefix="console-e2e-"))
for sub in ("nonebot/data", "qqbot-data/workspace/learn", "agentd", "backup"):
    (ROOT / sub).mkdir(parents=True, exist_ok=True)
(ROOT / "nonebot" / "config.yaml").write_text(
    "admin:\n  user_id: 'console'\nllm:\n  api: 'http://127.0.0.1:9/v1/chat/completions'\n  model: 'fake'\n",
    encoding="utf-8")
os.environ["QQBOT_ROOT"] = str(ROOT)

HERE = Path(__file__).resolve().parent
CODE = HERE.parent / "nonebot"
if not (CODE / "app_core").is_dir():
    CODE = HERE.parent
sys.path.insert(0, str(CODE))
sys.path.insert(0, str(HERE.parent))

from adapters.fake import FakeChannel  # noqa: E402
from app_core.chat import ChatCore  # noqa: E402
from app_core.engine import Engine, InboundMessage  # noqa: E402

PASS = FAIL = 0


def check(name, ok, extra=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("PASS " + name)
    else:
        FAIL += 1
        print("FAIL " + name + (" | " + str(extra) if extra else ""))


class FakeLLM:
    db = None

    async def complete(self, *a, **kw):
        return {"content": "在", "tool_calls": [], "reasoning_content": ""}

    async def stream(self, messages, tools_arg=None, on_text=None, **kw):
        if on_text:
            on_text("在")
        return {"content": "在", "tool_calls": [], "reasoning_content": ""}

    async def summarize(self, *a, **kw):
        return "{}"


async def fake_embed(texts):
    return [[0.1, 0.2, 0.3] for _ in texts]


async def main():
    core = ChatCore()
    await core.init_db()
    core.llm = FakeLLM()
    core.memory.embed = fake_embed
    core._schedule_summary = lambda session: None
    core._schedule_shadow = lambda *a, **k: None

    engine = Engine(core)
    ch = FakeChannel()
    out = await engine.handle(InboundMessage(session="private:console", text="在吗"))
    await ch.send(out)
    check("console 引擎有回复", bool(out.chunks), out.chunks)
    check("FakeChannel 收到一条", len(ch.sent) == 1, len(ch.sent))
    check("回复内容来自 fake LLM", "在" in "".join(out.chunks), out.chunks)

    hist = await core.history("private:console")
    roles = [h["role"] for h in hist]
    check("历史写入 user+assistant", roles == ["user", "assistant"], roles)

    con = sqlite3.connect(ROOT / "nonebot" / "data" / "chat.db")
    n = con.execute("SELECT COUNT(*) FROM messages WHERE session='private:console'").fetchone()[0]
    con.close()
    check("数据库落盘", n == 2, n)
    check("没有 import nonebot", "nonebot" not in sys.modules)


if __name__ == "__main__":
    asyncio.run(main())
    shutil.rmtree(ROOT, ignore_errors=True)
    print(f"\n=== {PASS}/{PASS + FAIL} 通过 ===")
    raise SystemExit(0 if FAIL == 0 else 1)
