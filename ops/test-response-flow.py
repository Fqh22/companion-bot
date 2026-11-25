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
from app_core.delivery import BUFFERED, DELIVERY_UNKNOWN
from app_core.engine import InboundMessage, OutboundMessage
from app_core.inbound_turns import InboundTurns
from app_core.turn import Turn
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
        failed_once = False

        async def typing(self, session, on):
            if on:
                await asyncio.Event().wait()

        async def send_part(self, session, part):
            if part.content == "第一段。":
                first_attempt.set()
                await release.wait()
            if part.content == "第二段。" and not self.failed_once:
                self.failed_once = True
                return False
            return True

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
    check("明确失败的中段补发后按原顺序记账", history == ["第一段。第二段。尾段。"], history)
    check("端到端计时落到实际首段确认", timing.marks["first_sent"] >= timing.marks["runner_start"])

    class UnknownChannel(Channel):
        def __init__(self):
            self.attempts = []

        async def send_part(self, session, part):
            self.attempts.append(part.content)
            return DELIVERY_UNKNOWN if part.content == "第二段。" else True

    unknown = UnknownChannel()
    problem = TurnTiming("test")
    with patch.object(pacing, "pace_seconds", return_value=0), patch.object(pacing, "gap_seconds", return_value=0):
        await TurnRunner(Engine(), unknown, deadline=3, timing=problem).run(InboundMessage("unknown", "测试"))
    check("送达未知不重发，拦住后续正文并标注中断",
          unknown.attempts[:2] == ["第一段。", "第二段。"] and len(unknown.attempts) == 3
          and "发送中断" in unknown.attempts[-1] and problem.status == "delivery_unknown"
          and "第二段" not in history[-1] and "尾段" not in history[-1], unknown.attempts)

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
    turn = SimpleNamespace(owner=True, user_text=text, store_text=None, t0=time.perf_counter(), prefetch={},
                           fetch_metrics={}, budget_exhausted=False, parallel=False)
    with (patch.object(Core, "_wants_search", return_value=True),
          patch.object(chat_turn.config, "load", return_value={"llm": {"prepare_parallel": {"weather": True}}})):
        await Core()._prepare_fetches(turn, text)
    check("只开启天气并发时预搜索继续执行", set(steps) == {"weather", "search"}, steps)
    check("慢预搜索按共享预算降级，天气结果保留",
          turn.fetch_metrics.get("search") == "timeout" and bool(turn.prefetch.get("weather")),
          turn.fetch_metrics)

    streamed = []
    requested = []

    async def stream(msgs, tools, on_text):
        allowed = {t["function"]["name"] for t in tools}
        assert {"web_search", "web_open"} <= allowed
        if not requested:
            await on_text("functions.web_search(")
            await on_text('{"query":"待核对的问题"})\n')
            name = "web_search"
        elif requested == ["web_search"]:
            await on_text("还没核实的中间结论。")
            name = "web_open"
        else:
            await on_text("原网页已经核实。")
            return {"content": "原网页已经核实。"}
        return {"tool_calls": [{"id": name, "function": {"name": name, "arguments": "{}"}}]}

    async def complete(*args):
        raise AssertionError("查证轮应沿用流式通道")

    async def execute(_, name, args, images):
        requested.append(name)
        return "只有搜索摘要" if name == "web_search" else "原网页正文"

    async def on_text(text):
        streamed.append(text)

    tools_mod = SimpleNamespace(TOOLS=[{"function": {"name": n}} for n in ("web_search", "web_open")],
                                execute=execute)
    output = await tool_loop.run(None, [], complete=complete, stream=stream, tools_mod=tools_mod,
                                 tools_banned=set(), max_rounds=3, tool_timeout=1,
                                 total_timeout=3, on_text=on_text)
    check("首轮工具参数和续轮中间结论均不外发", requested == ["web_search", "web_open"]
          and streamed == [output] and output == "原网页已经核实。", streamed)


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


async def inbound_bursts():
    seen = []
    started = asyncio.Event()
    release = asyncio.Event()

    async def handle(key, turn):
        seen.append((key, turn))
        if turn.texts == ["已开始"]:
            started.set()
            await release.wait()

    queue = InboundTurns(handle)
    quick = queue.submit("qq", Turn(texts=["今天几点下班？"],
                                    meta={"received_at": time.monotonic() - 0.95}))
    await asyncio.wait_for(quick, 0.3)
    check("普通单条文字走 0.9 秒等待窗口", len(seen) == 1)
    seen.clear()
    first = queue.submit("qq", Turn(texts=["我给你看个截图"]))
    await asyncio.sleep(1.2)
    same = queue.submit("qq", Turn(texts=["这是昨天的"], meta={"reply_to": "a", "quote_text": "几点？"}))
    other = queue.submit("feishu", Turn(texts=["另一通道"], meta={"received_at": time.monotonic() - 3}))
    await other
    check("默认窗口能合并间隔超过一秒的连发，两通道互不阻塞",
          first is same and [key for key, _ in seen] == ["feishu"])
    queue.submit("qq", Turn(texts=["帮我看下"]))
    await first
    merged = seen[-1][1]
    check("首条到末条原样按顺序合并并保留引用和最早收件时间",
          merged.texts == ["我给你看个截图", "这是昨天的", "帮我看下"]
          and merged.meta["reply_to"] == "a" and merged.meta["quote_text"] == "几点？"
          and merged.meta["input_count"] == 3
          and merged.meta["received_at"] < merged.meta["ready_at"] - 1)

    seen.clear()
    now = time.monotonic()
    task = queue.submit("qq", Turn(texts=["已开始"], meta={"received_at": now - 10}))
    await asyncio.wait_for(started.wait(), 1)
    for text, offset in (("我再补一句", -7.5), ("同一件事", -6.1), ("接着说", -4.5),
                         ("超过首条四秒", -3.3), ("隔一阵再说", -0.95)):
        queue.submit("qq", Turn(texts=[text], meta={"received_at": now + offset}))
    check("当前回复在途时先排队", len(seen) == 1)
    release.set()
    await task
    check("当前回复后的短时间补充合并，连续发到上限或隔太久就另起一轮",
          [turn.texts for _, turn in seen] == [["已开始"], ["我再补一句", "同一件事", "接着说"],
                                             ["超过首条四秒"], ["隔一阵再说"]])

    seen.clear()
    queue = InboundTurns(handle, text_gap=0)
    a = queue.submit("qq", Turn(texts=["一"]))
    queue.submit("qq", Turn(texts=["二"]))
    await a
    check("关闭文本等待后连发按独立输入处理", [turn.texts for _, turn in seen] == [["一"], ["二"]])

    seen.clear()
    queue = InboundTurns(handle)
    now = time.monotonic() - 3
    task = queue.submit("qq", Turn(texts=["这条"], meta={"received_at": now, "reply_to": "a"}))
    queue.submit("qq", Turn(texts=["另一条"], meta={"received_at": now + 0.1, "reply_to": "b"}))
    await task
    check("两条消息引用不同对象时分轮", [turn.meta["reply_to"] for _, turn in seen] == ["a", "b"])


async def inbound_images():
    seen = []

    async def handle(key, turn):
        seen.append((key, turn))

    for image_first in (True, False):
        ready = asyncio.Event()

        async def slow_image():
            await ready.wait()
            return "screenshot.jpg"

        queue = InboundTurns(handle, text_gap=0.05, media_gap=0.05)
        image = Turn(meta={"event_id": "image"})
        text = Turn(texts=["帮我看红框"])
        if image_first:
            task = queue.submit("a", image, image_loaders=(slow_image,))
            queue.submit("a", text)
        else:
            task = queue.submit("a", text)
            queue.submit("a", image, image_loaders=(slow_image,))
        await asyncio.sleep(0.08)
        blocked = not task.done()
        ready.set()
        await task
        check(f"慢图与说明仍为同一轮（先发{'图' if image_first else '文字'}）",
              blocked and len(seen) == 1 and seen[0][1].texts == ["帮我看红框"]
              and seen[0][1].images == ["screenshot.jpg"])
        seen.clear()

    await queue.submit("b", Turn(texts=["看引用"], meta={"reply_to": "image"}))
    await queue.submit("a", Turn(texts=["看引用"], meta={"reply_to": "image"}))
    check("引用原图跨轮仍有效，其他会话的同一个 ID 无法取图",
          [(key, turn.images) for key, turn in seen] == [("b", []), ("a", ["screenshot.jpg"])])

    seen.clear()
    with patch.object(InboundTurns, "REFERENCE_SECONDS", 0.01):
        queue = InboundTurns(handle, text_gap=0, media_gap=0)
        await queue.submit("a", Turn(meta={"event_id": "expires"}), image_loaders=(slow_image,))
        await asyncio.sleep(0.03)
        await queue.submit("a", Turn(texts=["看旧引用"], meta={"reply_to": "expires"}))
    check("引用索引到期后释放且不再次带出旧图",
          [turn.images for _, turn in seen] == [["screenshot.jpg"], []])

    async def missing():
        await asyncio.Event().wait()

    async def available():
        return "available.jpg"

    seen.clear()
    queue = InboundTurns(handle, text_gap=0, media_gap=0, image_timeout=0.04)
    a = queue.submit("a", Turn(texts=["看这两张图"]), image_loaders=(missing, available))
    await asyncio.wait_for(a, 0.3)
    mixed = seen[0][1]
    check("部分图片超时保留可用图，告诉模型有图没拿到",
          len(seen) == 1 and mixed.images == ["available.jpg"]
          and "未能读取" in mixed.image_status("看这两张图"))


async def visible_reply():
    from app_core.self_check import clean_reply

    names = {"kb_search", "web_search"}
    dsml = "<｜DSML｜function_calls><｜DSML｜invoke name=\"kb_search\">私有参数</｜DSML｜invoke></｜DSML｜function_calls>"
    payload = '{"name":"kb_search","arguments":{"query":"模拟私有参数"}}'
    fence = chr(96) * 3
    answer = "第一句。\n" + dsml + "\n" + fence + "json\n" + payload + "\n" + fence + "\n"
    answer += "[相关知识资料]\n旧笔记目录\n\n最后一句。"
    clean = clean_reply(answer, names)
    check("工具协议、机器调用 JSON 和内部知识块均不进入最终答复",
          clean.split() == ["第一句。", "最后一句。"], clean)
    check("原生工具标记和截断的调用 JSON 都被拦住",
          clean_reply("正文。<｜tool▁calls▁begin｜>参数<｜tool▁calls▁end｜>后半句。", names) == "正文。后半句。"
          and clean_reply('正文。\n{"tool_calls":[{"name":"kb_search"', names) == "正文。")
    ordinary_code = fence + 'json\n{"name":"Alice","count":2}\n' + fence
    shown_code = fence + "json\n" + payload + "\n" + fence
    check("保留正常 JSON 与用户明确索取的工具示例",
          clean_reply(ordinary_code, names) == ordinary_code
          and clean_reply(shown_code, names, show_tool_examples=True) == shown_code
          and clean_reply("<parameter>轨道编号</parameter>", names) == "<parameter>轨道编号</parameter>")

    calls = {"n": 0}
    streamed = []

    async def final_after_budget(msgs, tools, on_text):
        calls["n"] += 1
        if tools:
            await on_text("这行不能发。")
            return {"content": "这行不能发。", "tool_calls": [
                {"id": "again", "function": {"name": "kb_search", "arguments": "{}"}}]}
        await on_text(payload)
        return {"content": payload + "\n最终回复。"}

    async def execute(*args):
        return "资料"

    async def send(text):
        streamed.append(text)

    tools_mod = SimpleNamespace(TOOLS=[{"function": {"name": "kb_search"}}], execute=execute)
    result = await tool_loop.run(None, [], complete=None, stream=final_after_budget, tools_mod=tools_mod,
                                 tools_banned=set(), max_rounds=1, tool_timeout=1,
                                 total_timeout=2, on_text=send)
    check("工具轮数触顶后的最终回复也检查正文", calls["n"] == 2 and result == "最终回复。"
          and streamed == [result], streamed)

    async def broken(msgs, tools, on_text):
        await on_text("这行可能还会变。")
        return {"content": "这行可能还会变。", "stream_finished": False}

    streamed.clear()
    result = await tool_loop.run(None, [], complete=None, stream=broken, tools_mod=tools_mod,
                                 tools_banned=set(), max_rounds=1, tool_timeout=1,
                                 total_timeout=2, on_text=send)
    check("流未正常结束不会投递候选正文", not result and not streamed)


async def inbound_voice():
    seen = []

    async def handle(key, turn):
        seen.append(turn)

    async def slow_voice():
        await asyncio.sleep(0.045)
        return "补充的转写"

    async def unreadable():
        return ""

    queue = InboundTurns(handle, text_gap=0.025, media_gap=0.025)
    task = queue.submit("voice", Turn(texts=["先说这件事"]))
    queue.submit("voice", Turn(), voice_loader=slow_voice)
    queue.submit("voice", Turn(texts=["最后一句"]))
    await task
    check("文字、慢转写、文字合为一轮并保留收件顺序",
          len(seen) == 1 and seen[0].texts == ["先说这件事", "补充的转写", "最后一句"]
          and seen[0].meta["voice"], seen)
    task = queue.submit("voice", Turn(), voice_loader=unreadable)
    queue.submit("voice", Turn(texts=["还有这句"]))
    await task
    check("语音失败时保留同轮文字并标出缺失",
          seen[-1].texts == ["还有这句"] and seen[-1].meta["voice_unavailable"]
          and not seen[-1].meta["voice"], seen[-1])


async def main() -> int:
    await inbound_bursts()
    await inbound_voice()
    await inbound_images()
    await delivery()
    await lookups()
    await visible_reply()
    await progress()
    await pooled_embedding()
    return report()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
