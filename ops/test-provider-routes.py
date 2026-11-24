"""Provider routing contract using synthetic messages and local HTTP transport."""
from __future__ import annotations

import asyncio
import json
import os
from unittest.mock import patch

from harness import bootstrap, check, report, temp_root

os.environ["QQBOT_ROOT"] = str(temp_root("provider-routes-"))
bootstrap()

import httpx
from app_core import asr, config, llm, ocr, tts, web
from app_core.provider_routes import DEEPSEEK_CHAT, chat_routes, service_route, validate_services


async def main() -> int:
    calls = []

    def handle(request):
        payload = json.loads(request.content) if request.headers.get("content-type") == "application/json" else {}
        calls.append((str(request.url), request.headers.get("authorization"), payload))
        if payload.get("stream"):
            chunks = [
                {"choices": [{"delta": {"content": "收到", "reasoning_content": "内部"}}]},
                {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "a", "function": {"name": "search", "arguments": "{"}}]}}]},
                {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "}"}}]}, "finish_reason": "tool_calls"}]},
            ]
            return httpx.Response(200, text="".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n")
        if request.url.path == "/search":
            return httpx.Response(200, json={"results": [{"title": "资料", "url": "https://fixture.invalid/"}]})
        if request.url.path == "/ocr":
            return httpx.Response(200, json={"pages": [{"text": "测试文字"}]})
        if request.url.path == "/asr":
            return httpx.Response(200, json={"text": "测试转写"})
        if request.url.path == "/speak":
            return httpx.Response(200, content=b"x" * 2000)
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": '{"ok":true}'}}]})

    def client(route, summary_route):
        return llm.LLM(temp_root("provider-db-") / "test.db", api=route.api,
                       model="chat-model", temperature=1.15, max_tokens=1000, thinking=True,
                       summary_model="summary-model", summary_max=700, summary_retry="summary-model",
                       summary_thinking=False, route=route, summary_route=summary_route)

    messages = [{"role": "assistant", "content": "已查", "reasoning_content": "内部"}]
    tools = [{"type": "function", "function": {"name": "search"}}]
    a, s = chat_routes({"api": DEEPSEEK_CHAT}, {})
    legacy = client(a, s)
    check("旧配置的总结继续共用旧聊天端点", chat_routes({"api": "https://legacy.invalid/chat"}, {})[1] is None)
    current, summary = chat_routes(
        {"api": "https://chat.invalid/chat", "protocol": "openai_compatible", "key_ref": "chat"},
        {"api": "https://summary.invalid/chat", "protocol": "openai_compatible", "key_ref": "summary"})
    check("只切聊天协议时总结仍走 DeepSeek",
          chat_routes({"api": current.api, "protocol": current.protocol, "key_ref": "chat"}, {})[1].api == DEEPSEEK_CHAT)
    for opts in ({"api": "https://other.invalid/chat", "protocol": "deepseek"},
                 {"api": "https://other.invalid/chat", "protocol": "openai_compatible", "key_ref": "deepseek"}):
        try:
            chat_routes(opts, {})
        except ValueError:
            check("新地址不能复用默认 DeepSeek 凭证", True)
        else:
            check("新地址不能复用默认 DeepSeek 凭证", False)

    transport = httpx.MockTransport(handle)
    async_client = httpx.AsyncClient
    async with async_client(transport=transport) as http:
        with patch.object(llm.keys, "get", side_effect=lambda k: "fixture-" + k), patch.object(llm, "_http", return_value=http):
            text = []

            async def receive(delta):
                text.append(delta)

            out = await legacy.stream(messages, tools, receive)
            check("DeepSeek 请求字段逐项兼容", calls[-1] == (DEEPSEEK_CHAT, "Bearer fixture-deepseek", {
                "model": "chat-model", "messages": messages, "temperature": 1.15,
                "max_tokens": 1000, "thinking": {"type": "enabled"}, "stream": True,
                "stream_options": {"include_usage": True}, "tools": tools}))
            check("流式正文、推理和分片工具参数保留", text == ["收到"] and out["reasoning_content"] == "内部"
                  and out["tool_calls"][0]["function"] == {"name": "search", "arguments": "{}"})
            await legacy.summarize(messages, json_mode=True)
            check("旧总结端点、凭证、关闭思考与 JSON 模式保持一致", calls[-1] == (
                DEEPSEEK_CHAT, "Bearer fixture-deepseek", {"model": "summary-model", "messages": messages,
                "temperature": 1.15, "max_tokens": 700, "thinking": {"type": "disabled"},
                "response_format": {"type": "json_object"}}))
            alt = client(current, summary)
            await alt.stream(messages, tools)
            api, auth, fields = calls[-1]
            check("兼容模型走独立端点与凭证，过滤 DeepSeek 专用字段", (api, auth) == (current.api, "Bearer fixture-chat")
                  and fields["messages"] == [{"role": "assistant", "content": "已查"}]
                  and not {"thinking", "stream_options"} & fields.keys())
            result = await alt.summarize(messages, json_mode=True)
            check("总结走独立地址和凭证，保留 JSON 输出", json.loads(result) == {"ok": True}
                  and calls[-1][:2] == (summary.api, "Bearer fixture-summary")
                  and calls[-1][2]["response_format"] == {"type": "json_object"})

        services = {name: {"api": f"https://media.invalid/{path}", "key_ref": name}
                    for name, path in (("ocr", "ocr"), ("asr", "asr"), ("search", "search"),
                                       ("tts_deepseek", "speak"), ("speech_rewrite", "rewrite"))}
        validate_services(services)
        check("语音及 OCR 默认服务沿用原凭证", service_route("asr").key_ref == service_route("ocr").key_ref == "deepseek")
        with patch.object(llm.keys, "get", side_effect=lambda k: "fixture-" + k), patch.object(
                config, "load", return_value={"provider_routes": services}), patch.object(
                httpx, "AsyncClient", side_effect=lambda *a, **kw: async_client(transport=transport, **kw)), patch.object(
                asr, "duration", return_value=1), patch.object(tts, "REWRITE", True):
            media = temp_root("provider-media-") / "fixture.wav"
            media.write_bytes(b"fixture")
            check("搜索、语音转写、OCR、合成与改写的入口均使用独立路由",
                  "资料" in await web.search("测试") and (await asr.transcribe(media))["text"] == "测试转写"
                  and await ocr.ocr_text(media) == "测试文字" and len(await tts._pcm_deepseek("测试")) == 2000
                  and json.loads(await tts.spoken_script("测试")) == {"ok": True})
            check("外围服务凭证只发给各自端点", [(api.split("/")[-1], auth) for api, auth, _ in calls[-5:]] == [
                ("search", "Bearer fixture-search"), ("asr", "Bearer fixture-asr"), ("ocr", "Bearer fixture-ocr"),
                ("speak", "Bearer fixture-tts_deepseek"), ("rewrite", "Bearer fixture-speech_rewrite")])
    return report()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
