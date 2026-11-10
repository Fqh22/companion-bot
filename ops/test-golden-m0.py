#!/usr/bin/env python3
"""M0：10 条真实对话的结构重放（fixture 不进 Git）。"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

_fixture = os.environ.get("M0_GOLDEN")
FIXTURE = Path(_fixture) if _fixture else Path("/nonexistent/m0-golden.json")
ROOT = Path(tempfile.mkdtemp(prefix="m0-golden-"))
for sub in ("nonebot/data", "qqbot-data/workspace/learn", "backup"):
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

from app_core.chat import ChatCore  # noqa: E402
from app_core.delivery import Capability  # noqa: E402
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

    def __init__(self, text):
        self.text = text

    async def complete(self, *a, **kw):
        return {"content": self.text, "tool_calls": [], "reasoning_content": ""}

    async def stream(self, messages, tools_arg=None, on_text=None, **kw):
        if on_text:
            on_text(self.text)
        return {"content": self.text, "tool_calls": [], "reasoning_content": ""}

    async def summarize(self, *a, **kw):
        return "{}"


async def fake_embed(texts):
    return [[0.1, 0.2, 0.3] for _ in texts]


def norm(s):
    return re.sub(r"\s+", "", s or "")


async def main():
    if not FIXTURE.exists():
        print("SKIP 没有 M0 fixture：", FIXTURE)
        return 0
    cases = json.loads(FIXTURE.read_text(encoding="utf-8"))
    check("fixture 恰好 10 条", len(cases) == 10, len(cases))

    core = ChatCore()
    await core.init_db()
    core.memory.embed = fake_embed
    core._schedule_summary = lambda session: None
    core._schedule_shadow = lambda *a, **k: None

    for i, case in enumerate(cases):
        core.llm = FakeLLM(case["assistant"])
        engine = Engine(core)
        out = await engine.handle(
            InboundMessage(session="private:console", text=case["user"]),
            caps=Capability(name="qq"))
        joined = "".join(out.chunks)
        check(f"case {i + 1}: 有分段", bool(out.chunks), out.chunks)
        check(f"case {i + 1}: 内容不丢", norm(joined) == norm(case["assistant"]),
              f"{len(joined)} vs {len(case['assistant'])}")

    rows = sqlite3.connect(ROOT / "nonebot" / "data" / "chat.db").execute(
        "SELECT COUNT(*) FROM messages WHERE session='private:console'").fetchone()[0]
    check("10 轮写入 20 条历史", rows == 20, rows)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    try:
        rc = asyncio.run(main())
    finally:
        shutil.rmtree(ROOT, ignore_errors=True)
    print(f"\n=== {PASS}/{PASS + FAIL} 通过 ===")
    raise SystemExit(rc)
