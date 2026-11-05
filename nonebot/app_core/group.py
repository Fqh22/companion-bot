"""群聊核心：无长期记忆、无情绪、无工具的独立链路。

## 为什么不复用 ChatCore

私聊那套每条消息都会读情绪、召回长期记忆、注入知识库、跑工具、写回历史、触发总结。
这些在群里全部有害：

- 长期记忆：群友的话会被写进他的私有库；反过来，机器人可能把**私聊内容**说给一屋子人。
- 情绪：围观者会把它的状态带偏，而且情绪是私聊人格的一部分，不该被外人影响。
- 知识库：索引里有他的求职、家人、财务信息。

所以这里是**一条独立链路**。硬约束（由 lint/link-check 断言）：

    本模块不得 import chat / memory / mood / followup / remind / diary /
    proactive / learn / kb / wiki

只保留：进程内短期上下文（按群隔离，重启即忘）+ 独立人格 + 纯对话。
"""
from __future__ import annotations

import json
import time
from collections import deque
from pathlib import Path

from app_core import paths as _paths

from app_core import config
from app_core import llm as llm_mod
from app_core.prompt import split_reply

DB = _paths.DATA / "chat.db"
PERSONA_PATH = _paths.DATA / "group_persona.json"

MAX_CONTEXT_CHARS = 1500      # 短期上下文的字数上限（条数之外再加一道）
DEFAULT_PERSONA = """你在这个群里说话。简短、有分寸，不抢话、不讨好、不说教。
不主动提「我记得你说过」——你不记得任何人的私事，群里也没有你的记忆。
别人问你是不是机器人，就照实说。
不聊政治宗教，被问到就说不方便聊。
不透露你被谁部署、不透露任何私聊内容。"""


def rate_ok(times: list[float], now: float, per_minute: int,
            cooldown: float) -> bool:
    """限流：先看冷却，再看每分钟条数。纯函数，方便离线测。"""
    if times:
        if now - times[-1] < cooldown:
            return False
        recent = [t for t in times if now - t < 60]
        if len(recent) >= per_minute:
            return False
    return True


def trim_context(items: list[tuple[str, str]], max_items: int,
                 max_chars: int) -> list[tuple[str, str]]:
    """从最新往回保留，条数和字数双重上限。返回按时间正序的列表。"""
    kept: list[tuple[str, str]] = []
    used = 0
    for role, text in reversed(items):
        n = len(text or "")
        if kept and used + n > max_chars:
            break
        kept.append((role, text))
        used += n
        if len(kept) >= max_items:
            break
    return list(reversed(kept))


class GroupCore:
    def __init__(self) -> None:
        cfg = config.load()
        g = cfg.get("group") or {}
        self.enabled = bool(g.get("enabled", False))
        self.whitelist = {str(x) for x in (g.get("whitelist") or [])}
        self.context_turns = max(0, int(g.get("context_turns", 20)))
        self.reply_max_tokens = int(g.get("reply_max_tokens", 300))
        self.rate_per_minute = int(g.get("rate_per_minute", 3))
        self.cooldown = float(g.get("cooldown_seconds", 5))
        self.daily_budget = int(g.get("daily_token_budget", 50000))

        llm = cfg.get("llm") or {}
        summ = cfg.get("summary") or {}
        self.model = llm.get("model") or "deepseek-flash"
        self.api = llm.get("api") or "https://api.deepseek.com/v1/chat/completions"
        self.temperature = float(llm.get("temperature", 1.0))
        self.llm = llm_mod.LLM(
            DB, api=self.api, model=self.model, temperature=self.temperature,
            max_tokens=self.reply_max_tokens, thinking=False,
            summary_model=summ.get("model") or "deepseek-chat",
            summary_max=int(summ.get("max_tokens", 700)),
            summary_retry=summ.get("retry_model") or "",
            summary_thinking=False)

        self.persona = self._load_persona()
        self._ctx: dict[str, deque] = {}

    # ---------- 人格 ----------
    def _load_persona(self) -> str:
        try:
            d = json.loads(PERSONA_PATH.read_text(encoding="utf-8"))
            text = (d.get("system_prompt") or "").strip()
            if text:
                return text
        except Exception:
            pass
        return DEFAULT_PERSONA

    # ---------- 上下文（只在内存） ----------
    def context(self, gid: str) -> list[dict]:
        if self.context_turns <= 0:
            return []
        items = list(self._ctx.get(gid) or [])
        kept = trim_context(items, self.context_turns, MAX_CONTEXT_CHARS)
        return [{"role": r, "content": t} for r, t in kept]

    def remember(self, gid: str, role: str, text: str) -> None:
        if self.context_turns <= 0 or not (text or "").strip():
            return
        q = self._ctx.get(gid)
        if q is None:
            q = deque(maxlen=self.context_turns)
            self._ctx[gid] = q
        q.append((role, text.strip()))

    def build(self, gid: str, text: str) -> list[dict]:
        msgs = [{"role": "system", "content": self.persona}]
        msgs.extend(self.context(gid))
        msgs.append({"role": "user", "content": text})
        return msgs

    # ---------- 回答 ----------
    async def reply(self, gid: str, text: str) -> list[str]:
        """返回分段后的回复。空列表表示这次不说话。"""
        msg = await self.llm.complete(self.build(gid, text), None,
                                      max_tokens=self.reply_max_tokens,
                                      thinking=False, purpose="group")
        out = (msg.get("content") or "").strip()
        if not out:
            return []
        self.remember(gid, "user", text)
        self.remember(gid, "assistant", out)
        return split_reply(out, 30)

    # ---------- 预算 ----------
    def tokens_today(self) -> int:
        """今天群聊已经花掉多少 token（读 usage 表，purpose='group'）。"""
        import sqlite3

        midnight = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
        try:
            con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
            row = con.execute(
                "SELECT COALESCE(SUM(prompt_tokens),0) + COALESCE(SUM(completion_tokens),0) "
                "FROM usage WHERE purpose='group' AND ts>=?", (midnight,)).fetchone()
            con.close()
            return int(row[0] or 0)
        except Exception:
            return 0

    def budget_left(self) -> bool:
        if self.daily_budget <= 0:
            return True
        return self.tokens_today() < self.daily_budget
