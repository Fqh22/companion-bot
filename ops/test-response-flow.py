"""回复流程验收。使用脱敏对话和可控的假平台、模型与取数。"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import patch

from harness import bootstrap, check, report, temp_root

import os
os.environ["QQBOT_ROOT"] = str(temp_root("reply-flow-"))
bootstrap()
from app_core import chat_turn, embed_provider, pacing, tool_loop
from app_core.delivery import BUFFERED
from app_core.engine import InboundMessage, OutboundMessage
from app_core.round_timing import TurnTiming, current
from app_core.runner import TurnRunner


async def delivery():
    producer_done = asyncio.Event()
    first_attempt = asyncio.Event()
    release = asyncio.Event()
    history = []

    async def record(session, text, **kwargs):
        history.append(text)

    class Engine:
        core = SimpleNamespace(record_delivery=record)

        async def handle(self, msg, on_sentence=None, **kwargs):
            await on_sentence("第一段。")
            await on_sentence("第二段。")
            producer_done.set()
            return OutboundMessage(chunks=["尾段。"])

    class Channel:
        forms = {"text"}
        defer_text = False

        async def typing(self, session, on):
            if on:
                await asyncio.Event().wait()

        async def send_part(self, session, part):
            if part.content == "第一段。":
                first_attempt.set()
                await release.wait()
            return part.content != "第二段。"

    timing = TurnTiming("test")
    with patch.object(pacing, "pace_seconds", return_value=0), patch.object(pacing, "gap_seconds", return_value=0):
        run = asyncio.create_task(TurnRunner(Engine(), Channel(), deadline=3, timing=timing).run(
            InboundMessage("test", "短租这周先别问")))
        try:
            await asyncio.wait_for(producer_done.wait(), 0.7)
            await asyncio.wait_for(first_attempt.wait(), 0.7)
            check("typing 卡住和首段投递在途时模型继续产生后续正文", not run.done())
        finally:
            release.set()
            await run
    check("历史只写实际送达，顺序保留", history == ["第一段。尾段。"], history)
    check("端到端计时落到实际首段确认", timing.marks["first_sent"] >= timing.marks["runner_start"])

    release.clear()
    first_attempt.clear()
    earlier = asyncio.create_task(TurnRunner(Engine(), Channel(), deadline=3).run(
        InboundMessage("queue", "先回答这一句")))
    later_timing = TurnTiming("test")

    class NextEngine:
        async def handle(self, msg, **kwargs):
            return OutboundMessage(chunks=["后一句。"])

    try:
        await asyncio.wait_for(first_attempt.wait(), 0.7)
        later = asyncio.create_task(TurnRunner(NextEngine(), Channel(), timing=later_timing).run(
            InboundMessage("queue", "我再补一句")))
        await asyncio.sleep(0)
        check("上一轮在送达时下一轮进入同会话队列",
              "runner_queued" in later_timing.marks and "runner_start" not in later_timing.marks)
    finally:
        release.set()
        await earlier
    await later

    class VoiceChannel(Channel):
        defer_text = True

        async def send_part(self, session, part):
            return BUFFERED

    with patch.object(pacing, "pace_seconds", side_effect=AssertionError("缓冲不该打字等待")):
        status = await TurnRunner(Engine(), VoiceChannel())._pace_and_send("voice", "朗读这段", 2, time.monotonic())
    check("语音缓冲无人工停顿", status == BUFFERED)


async def lookups():
    steps = []

    class Core(chat_turn.ChatTurnMixin):
        first_token_budget = 0.05
        reply_deadline = 45

        async def _prepare_search(self, turn, text):
            steps.append("search")
            await asyncio.sleep(0.2)

        async def _fetch_weather(self, turn):
            steps.append("weather")
            return [(0, "天气测试")]

    text = "现在天气情况怎么样？"
    turn = SimpleNamespace(owner=True, user_text=text, t0=time.perf_counter(), prefetch={},
                           fetch_metrics={}, budget_exhausted=False, parallel=False)
    with patch.object(chat_turn.config, "load", return_value={"llm": {"prepare_parallel": {"weather": True}}}):
        await Core()._prepare_fetches(turn, text)
    check("只开启天气并发时预搜索继续执行", set(steps) == {"weather", "search"}, steps)
    check("慢预搜索按共享预算降级，天气结果保留",
          turn.fetch_metrics.get("search") == "timeout" and bool(turn.prefetch.get("weather")),
          turn.fetch_metrics)

    streamed = []
    calls = []

    async def stream(msgs, tools, on_text):
        calls.append(tools)
        if tools:
            return {"tool_calls": [{"id": "t", "function": {"name": "web_search", "arguments": "{}"}}]}
        await on_text("查到了。")
        return {"content": "查到了。"}

    async def complete(*args):
        raise AssertionError("查到证据后应流式回复")

    async def execute(*args):
        return "[1] 查到测试证据"

    async def on_text(text):
        streamed.append(text)

    tools_mod = SimpleNamespace(TOOLS=[{"function": {"name": "web_search"}}], execute=execute)
    output = await tool_loop.run(None, [], complete=complete, stream=stream, tools_mod=tools_mod,
                                 tools_banned=set(), max_rounds=3, tool_timeout=1,
                                 total_timeout=3, on_text=on_text)
    check("单次搜索成功后正文继续流式", calls[-1] is None and streamed == [output], streamed)


async def progress():
    messages = []

    class Engine:
        async def handle(self, msg, on_sentence=None, **kwargs):
            timing = current.get()
            timing.checking += 1
            try:
                await asyncio.sleep(0.08)
            finally:
                timing.checking -= 1
            return OutboundMessage(chunks=["核实完了。"])

    class Channel:
        forms = {"text"}

        async def send_part(self, session, part):
            messages.append(part.content)
            return True

        async def typing(self, session, on):
            pass

    timing = TurnTiming("test")
    runner = TurnRunner(Engine(), Channel(), timing=timing)
    runner.PROGRESS_AFTER = 0.01
    await runner.run(InboundMessage("lookup", "帮我查一下"))
    check("真实取数等待只发一次进度，独立于正文送达",
          messages == ["我查一下。", "核实完了。"] and
          timing.marks["progress_sent"] < timing.marks["first_sent"], messages)


async def pooled_embedding():
    class Client:
        created = 0

        def __init__(self, **kwargs):
            Client.created += 1

        async def post(self, *args, **kwargs):
            return SimpleNamespace(status_code=200, raise_for_status=lambda: None,
                                   json=lambda: {"data": [{"embedding": [1.0]}]})

    with patch("httpx.AsyncClient", Client):
        for text in ("第一条", "第二条"):
            await embed_provider.embed([text], api="https://fixture.invalid", model="fixture", key="fixture")
        check("连续 embedding 共用 HTTP 客户端", Client.created == 1)
        await embed_provider.close()


async def main() -> int:
    await delivery()
    await lookups()
    await progress()
    await pooled_embedding()
    return report()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
