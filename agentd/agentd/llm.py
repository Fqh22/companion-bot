"""LLM 客户端：所有调用都记账（§四 llm_calls）。

决策 / 规划 / 压缩 / 重锚定走同一个入口，保证预算统计不漏。
"""
from __future__ import annotations

import json
import re
import time

import httpx

from .config import Config
from .store import Store


def extract_json(text: str) -> dict:
    """从模型输出里抠出第一个 JSON 对象。抠不出来返回 {}。

    容错：模型偶尔会在 token 上限处被截断（长命令最容易），这时 JSON 缺尾巴。
    与其判「不是工具调用」白跑一轮，不如把尾巴补齐再试一次。
    """
    if not text:
        return {}
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    raw = m.group(1) if m else None
    if raw is None:
        i, j = text.find("{"), text.rfind("}")
        raw = text[i:j + 1] if 0 <= i < j else (text[text.find("{"):] if "{" in text else "")
    for candidate in (raw, _repair(raw)):
        if not candidate:
            continue
        try:
            data = json.loads(candidate)
            if isinstance(data, dict):
                return data
        except Exception:  # noqa: BLE001
            continue
    return {}


def _repair(raw: str) -> str:
    """补齐被截断的 JSON：先闭合字符串，再补齐括号。"""
    s = (raw or "").strip()
    if not s.startswith("{"):
        return ""
    in_str, esc = False, False
    for ch in s:
        if esc:
            esc = False
            continue
        if ch == "\\":
            esc = True
        elif ch == '"':
            in_str = not in_str
    if in_str:
        s += '"'
    s += "]" * max(0, s.count("[") - s.count("]"))
    s += "}" * max(0, s.count("{") - s.count("}"))
    return s


class LLM:
    """真调用 DeepSeek。测试时用 tests 里的 FakeLLM 替换。"""

    def __init__(self, cfg: Config, store: Store, client: httpx.AsyncClient | None = None) -> None:
        self.cfg = cfg
        self.store = store
        self._client = client

    async def chat(self, messages: list[dict], purpose: str, *,
                   task_id: int | None = None, step_id: int | None = None,
                   max_tokens: int = 900, temperature: float = 0.3,
                   thinking: bool = False) -> str:
        if not self.cfg.api_key:
            raise RuntimeError("缺少 deepseek key（AGENTD_KEYS / keys.json）")
        body = {
            "model": self.cfg.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
        }
        if not thinking:
            body["thinking"] = {"type": "disabled"}
        t0 = time.time()
        client = self._client or httpx.AsyncClient(timeout=120)
        try:
            r = await client.post(
                self.cfg.api,
                headers={"Authorization": f"Bearer {self.cfg.api_key}",
                         "Content-Type": "application/json"},
                json=body)
            r.raise_for_status()
            data = r.json()
        finally:
            if self._client is None:
                await client.aclose()
        msg = (data.get("choices") or [{}])[0].get("message") or {}
        text = msg.get("content") or ""
        usage = data.get("usage") or {}
        self.store.record_call(
            task_id, purpose,
            int(usage.get("prompt_tokens") or _est(messages)),
            int(usage.get("completion_tokens") or _est([{"content": text}])),
            int((time.time() - t0) * 1000), step_id)
        return text


def _est(messages: list[dict]) -> int:
    """没有 usage 时按字符估：中文约 1 token/字，英文约 4 字符/token。"""
    n = 0
    for m in messages:
        c = m.get("content")
        if isinstance(c, list):
            c = " ".join(str(x.get("text", "")) for x in c if isinstance(x, dict))
        s = str(c or "")
        zh = sum(1 for ch in s if "\u4e00" <= ch <= "\u9fff")
        n += zh + (len(s) - zh) // 4
    return max(1, n)
