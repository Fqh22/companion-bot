"""LLM 客户端：模型调用、重试、流式、用量记账。

从 chat.py 抽出来的原因：这是唯一和"编排"无关、且规则最独立的一块
（超时/重试/截断扩容/缓存计价/流式）。抽出来以后 chat.py 只剩编排。

不依赖 nonebot，也不依赖 ChatCore；DB 路径由构造参数传入，方便测试。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

import aiosqlite
import httpx

log = logging.getLogger("qqbot.llm")

KEYS = Path("/opt/qqbot-data/keys.json")

# 这些状态码值得重试；其它 4xx 重试只是把同样的错误再犯三次
RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}
LLM_RETRIES = 2

_HTTP_TIMEOUT = None
_http_client = None
_http_cls = None
_key_cache: tuple[float, str] | None = None
_key_warned = False


def deepseek_key() -> str:
    """读 API key，按文件 mtime 缓存。

    每次请求都读盘不止浪费，关键是：文件损坏时它会安静地返回空串，
    然后在服务端 401 重试三次——日志里看不出是 key 的问题。
    """
    global _key_cache
    try:
        mtime = KEYS.stat().st_mtime
        if _key_cache and _key_cache[0] == mtime:
            return _key_cache[1]
        key = json.loads(KEYS.read_text(encoding="utf-8")).get("deepseek", "") or ""
    except Exception as e:  # noqa: BLE001
        global _key_warned
        if not _key_warned:
            log.error("[llm] 读取 keys.json 失败：%s", e)
            _key_warned = True
        return ""
    if not key:
        if not _key_warned:
            log.error("[llm] keys.json 里没有 deepseek key，所有请求都会 401")
            _key_warned = True
    _key_cache = (mtime, key)
    return key


def _http():
    """共享的 httpx 客户端：省掉每次请求的 TLS 握手（约 100-200ms）。

    「httpx.AsyncClient 被换掉就重建」是为了让测试里的 MockTransport 生效：
    测试会 patch 这个类，缓存必须跟着失效。
    """
    global _http_client, _http_cls, _HTTP_TIMEOUT
    if _HTTP_TIMEOUT is None:
        _HTTP_TIMEOUT = httpx.Timeout(90.0, connect=10.0)
    cls = httpx.AsyncClient
    if _http_client is None or _http_client.is_closed or cls is not _http_cls:
        _http_client = cls(timeout=_HTTP_TIMEOUT,
                           limits=httpx.Limits(max_connections=8,
                                               max_keepalive_connections=4))
        _http_cls = cls
    return _http_client


def _retryable(err: Exception) -> bool:
    """能不能重试，看的是「再发一次有没有可能成功」。"""
    if isinstance(err, (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout,
                        httpx.WriteError, httpx.PoolTimeout, httpx.RemoteProtocolError)):
        return True
    if isinstance(err, httpx.HTTPStatusError):
        return err.response.status_code in RETRY_STATUS
    return False


def _err_text(err: Exception) -> str:
    if isinstance(err, httpx.HTTPStatusError):
        body = (err.response.text or "")[:200].replace("\n", " ")
        return f"HTTP {err.response.status_code}: {body}"
    return f"{type(err).__name__}: {err}"


class LLM:
    def __init__(self, db: Path, *, api: str, model: str, temperature: float,
                 max_tokens: int, thinking: bool, summary_model: str,
                 summary_max: int, summary_retry: str, summary_thinking: bool):
        self.db = db
        self.api = api
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.thinking = thinking
        self.summary_model = summary_model
        self.summary_max = summary_max
        self.summary_retry = summary_retry
        self.summary_thinking = summary_thinking
        # 可替换，测试里不必真读 /opt 下的 keys.json
        self.key_fn = deepseek_key

    # ---------- 非流式 ----------
    async def complete(self, messages: list[dict], tools_arg=None,
                       max_tokens: int | None = None, model: str | None = None,
                       retries: int = LLM_RETRIES, thinking: bool | None = None,
                       purpose: str = "chat",
                       temperature: float | None = None) -> dict:
        """temperature=None 用客户端默认（聊天档）；只有批处理类脚本会显式压低，
        用来让「同一段输入重跑两次结果一致」——线上路径一律不传。"""
        think = self.thinking if thinking is None else thinking
        payload = {"model": model or self.model, "messages": messages,
                   "temperature": self.temperature if temperature is None else temperature,
                   "max_tokens": max_tokens or self.max_tokens,
                   "thinking": {"type": "enabled" if think else "disabled"}}
        if tools_arg:
            payload["tools"] = tools_arg
        key = self.key_fn()
        if not key:
            # 没有 key 时发出去的请求连 header 都不合法，报错还看不懂
            raise RuntimeError("deepseek key 未配置（/opt/qqbot-data/keys.json）")
        headers = {"Authorization": f"Bearer {key}"}
        last_err: Exception | None = None
        for attempt in range(retries + 1):
            try:
                t0 = time.perf_counter()
                r = await _http().post(self.api, headers=headers, json=payload)
                r.raise_for_status()
                data = r.json()
                latency = int((time.perf_counter() - t0) * 1000)
                try:
                    u = data.get("usage") or {}
                    await self.record_usage(payload["model"], u, latency, purpose)
                except Exception:
                    pass
                ch = (data.get("choices") or [{}])[0]
                msg = ch.get("message") or {}
                content = (msg.get("content") or "").strip()
                # finish_reason=length 就是被 max_tokens 截断。两种情形都要处理：
                #   a) 思考吃光预算，content 为空（会变成兜底「在」）
                #   b) 正文说到一半被切断（更隐蔽，用户看到半句话）
                if ch.get("finish_reason") == "length" and attempt == 0:
                    log.warning("输出被 max_tokens=%s 截断，加倍重试（已有正文 %d 字，tools=%d）",
                                payload["max_tokens"], len(content),
                                len(msg.get("tool_calls") or []))
                    payload["max_tokens"] = min(2000, int(payload["max_tokens"]) * 2)
                    continue
                fr = ch.get("finish_reason")
                if fr == "length":
                    log.warning("二次仍被截断，接受残缺输出（%d 字）", len(content))
                elif fr not in (None, "stop", "tool_calls"):
                    log.warning("finish_reason=%s，输出可能被过滤（%d 字）", fr, len(content))
                return msg
            except Exception as e:  # noqa: BLE001
                last_err = e
                if not _retryable(e):
                    # 400 参数错 / 401 key 失效 / 402 余额不足：重试只是把同样的
                    # 错误再犯三次，还会把「今天只会说在」的真实原因埋掉。
                    log.error("[llm] 不可重试的错误（%s）：%s", payload["model"], _err_text(e))
                    raise
                if attempt < retries:
                    log.warning("[llm] 第 %d 次失败，重试：%s", attempt + 1, _err_text(e))
                    await asyncio.sleep(0.8 * (attempt + 1))
        log.error("[llm] 重试耗尽：%s", _err_text(last_err))
        raise last_err  # type: ignore[misc]

    # ---------- 流式 ----------
    async def stream(self, messages: list[dict], tools_arg=None, on_text=None,
                     max_tokens: int | None = None,
                     thinking: bool | None = None) -> dict:
        """流式调用。边收边把文本增量交给 on_text，结束时返回完整 message。

        流式不重试：已经吐出去的文字收不回来。首字之前的失败由上层回退非流式处理。

        thinking=None 用默认；False 给闲聊轮——思考模式先规划结构再写，
        天然产出「总—分—总」，还会让 temperature 失效。
        """
        think = self.thinking if thinking is None else bool(thinking)
        payload = {"model": self.model, "messages": messages,
                   "temperature": self.temperature,
                   "max_tokens": max_tokens or self.max_tokens,
                   "thinking": {"type": "enabled" if think else "disabled"},
                   "stream": True,
                   # 流式默认不回 usage，不要求的话 /usage 里就只剩非流式调用的账
                   "stream_options": {"include_usage": True}}
        if tools_arg:
            payload["tools"] = tools_arg

        content, reasoning = "", ""
        calls: dict[int, dict] = {}
        finished = False
        finish_reason = ""
        usage: dict | None = None
        t0 = time.perf_counter()

        key = self.key_fn()
        if not key:
            raise RuntimeError("deepseek key 未配置（/opt/qqbot-data/keys.json）")
        client = _http()
        async with client.stream(
                "POST", self.api,
                headers={"Authorization": f"Bearer {key}",
                         "Content-Type": "application/json"},
                json=payload) as r:
            r.raise_for_status()
            async for line in r.aiter_lines():
                if not line.startswith("data:"):
                    continue
                raw = line[5:].strip()
                if not raw:
                    continue
                if raw == "[DONE]":
                    finished = True
                    continue
                try:
                    chunk = json.loads(raw)
                except Exception:
                    continue
                if chunk.get("usage"):
                    usage = chunk["usage"]
                choice = (chunk.get("choices") or [{}])[0]
                if choice.get("finish_reason"):
                    finish_reason = str(choice["finish_reason"])
                    finished = True
                delta = choice.get("delta") or {}
                if delta.get("reasoning_content"):
                    reasoning += delta["reasoning_content"]
                if delta.get("content"):
                    content += delta["content"]
                    if on_text:
                        try:
                            await on_text(delta["content"])
                        except Exception:
                            pass
                for tc in (delta.get("tool_calls") or []):
                    try:
                        idx = int(tc.get("index", 0))
                    except Exception:
                        idx = 0
                    slot = calls.setdefault(idx, {
                        "id": "", "type": "function",
                        "function": {"name": "", "arguments": ""}})
                    if tc.get("id"):
                        slot["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        slot["function"]["name"] += fn["name"]
                    if fn.get("arguments"):
                        slot["function"]["arguments"] += fn["arguments"]

        if usage:
            try:
                await self.record_usage(self.model, usage,
                                        int((time.perf_counter() - t0) * 1000), "chat")
            except Exception:
                pass

        msg = {"content": content}
        if reasoning:
            msg["reasoning_content"] = reasoning
        if calls:
            msg["tool_calls"] = [calls[i] for i in sorted(calls)]
        # 诊断：流没正常收尾时要能从日志看出来（用户在 QQ 里只会看到半句话）
        if not finished:
            log.warning("流式响应未正常结束：content=%d 字，finish=%r，tools=%d",
                        len(content), finish_reason, len(calls))
        elif finish_reason == "length":
            log.warning("流式输出被 max_tokens 截断：content=%d 字（流式不能重试）",
                        len(content))
        else:
            log.info("流式结束 finish=%s content=%d 字 tools=%d",
                     finish_reason or "done", len(content), len(calls))
        return msg

    # ---------- 总结 ----------
    async def summarize(self, messages: list[dict], max_tokens: int | None = None,
                        temperature: float | None = None) -> str:
        """总结类任务。

        重试策略：主模型重试一次；只有配置了**不同**的备用模型才再换一个。
        之前两个模型名相同时会各跑 3 次，等于同一个错误犯 6 遍。
        """
        mt = max_tokens or self.summary_max
        models = [self.summary_model]
        if self.summary_retry and self.summary_retry != self.summary_model:
            models.append(self.summary_retry)
        for i, model in enumerate(models):
            try:
                msg = await self.complete(
                    messages, None, mt, model=model, thinking=self.summary_thinking,
                    retries=1 if i == 0 else 0, purpose="summary",
                    temperature=temperature)
                text = (msg.get("content") or "").strip()
                if text:
                    return text
            except Exception as e:  # noqa: BLE001
                log.warning("总结模型 %s 失败：%s", model, _err_text(e))
                continue
        return ""

    # ---------- 记账 ----------
    async def record_usage(self, model: str, usage: dict, latency_ms: int = 0,
                           purpose: str = "") -> None:
        pt = int(usage.get("prompt_tokens") or 0)
        ct = int(usage.get("completion_tokens") or 0)
        if not (pt or ct):
            return
        # 缓存命中省的是真金白银，记下来才看得见效果
        hit = int(usage.get("prompt_cache_hit_tokens") or 0)
        miss = int(usage.get("prompt_cache_miss_tokens") or 0)
        async with aiosqlite.connect(self.db) as db:
            await db.execute(
                "INSERT INTO usage(ts, model, prompt_tokens, completion_tokens, latency_ms,"
                " cache_hit, cache_miss, purpose) VALUES (?,?,?,?,?,?,?,?)",
                (time.time(), model, pt, ct, latency_ms, hit, miss, purpose))
            await db.commit()
