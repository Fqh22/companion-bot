#!/usr/bin/env python3
"""批次 2 护栏自检：检索门控 / 确定性预筛 / 每轮状态归属 / 配置默认值。

不依赖第三方库、不碰生产数据。除了正例，还故意注入反例确认守门员会红——
避免「规则写了但悄悄失效」这类静默失败。
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CODE = Path(os.environ.get("QQBOT_CODE_DIR", str(ROOT / "nonebot"))).resolve()
sys.path.insert(0, str(CODE))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

from app_core import contradict, recall_gate  # noqa: E402

TOTAL = 0
FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    global TOTAL
    TOTAL += 1
    print(("PASS " if cond else "FAIL ") + name + (f" | {extra}" if extra else ""))
    if not cond:
        FAILS.append(name)


def gate_checks() -> None:
    d = recall_gate.decide
    check("门控：显式回忆词判需要", d("还记得上次说的那件事吗")[0])
    check("门控：回指上文判需要", d("那个后来怎么样了", refers_previous=True)[0])
    check("门控：纯寒暄判不需要", not d("哈哈")[0])
    check("门控：命令判不需要", not d("/status")[0])
    check("门控：空文本判不需要", not d("")[0])
    check("门控：带图空文本保守放行", d("", has_images=True)[0])
    check("门控：拿不准一律放行", d("今天天气不错，随便聊聊")[0])
    check("门控：原因可写进影子日志", bool(d("还记得吗")[1]))


def prefilter_checks() -> None:
    cands = [
        {"doc_id": "a", "text": "他在找工作", "score": 0.8,
         "meta": {"create_time": 100, "source_range": "s:1-8"}},
        {"doc_id": "b", "text": "他上周入职了", "score": 0.9,
         "meta": {"create_time": 90, "source_range": "s:1-8"}},
        {"doc_id": "c", "text": "他喜欢美式", "score": 0.7,
         "meta": {"create_time": 50, "source_range": "s:9-16"}},
    ]
    kept, dropped = contradict.prefilter(
        cands, "他上周入职了", {"create_time": 100, "source_range": "s:1-8"})
    check("预筛：完全相同的文本交给去重，不送模型",
          any(x["reason"] == "same-text" for x in dropped))
    check("预筛：同一水位区间不自相取代",
          any(x["reason"] == "same-range" for x in dropped))
    check("预筛：语义冲突的候选仍留给模型",
          [x["doc_id"] for x in kept] == ["c"], str([x["doc_id"] for x in kept]))

    c2 = [{"doc_id": "d", "text": "另一件事", "score": 0.8,
           "meta": {"create_time": 200, "source_range": "s:2-3"}}]
    kept2, dropped2 = contradict.prefilter(
        c2, "新事实", {"create_time": 100, "source_range": "s:9-16"})
    check("预筛：时序倒挂不送模型",
          not kept2 and dropped2[0]["reason"] == "time-inverted")

    kept3, dropped3 = contradict.prefilter(cands[:1], "完全不同的新事", {})
    check("预筛：没有新 meta 时不误杀", len(kept3) == 1 and not dropped3)
    check("四道闸未被预筛破坏：低相似度不改",
          contradict.should_supersede(
              {"action": "supersede", "why": "旧过期"}, 0.5, 0)[0] is False)


_BANNED = ("self.memory_trace", "self._turn", "self.trace", "self._current_turn")


def _state_violations(files: dict[str, str]) -> list[str]:
    joined = (files["chat.py"] + files["chat_turn.py"] + files["chat_turn_base.py"]
              + files["chat_turn_commit.py"] + files["memory_port.py"])
    return [b for b in _BANNED
            if re.search(rf"\b{re.escape(b)}\s*=", joined)]


def state_checks() -> None:
    core = CODE / "app_core"
    names = ("chat.py", "chat_turn.py", "chat_turn_base.py", "chat_turn_commit.py",
             "memory_port.py", "runner.py")
    files = {n: (core / n).read_text(encoding="utf-8") for n in names}
    check("每轮状态不挂回实例", not _state_violations(files),
          str(_state_violations(files)))
    check("MemoryPort 让调用方传自己的 trace",
          "trace: list[dict] | None = None" in files["memory_port.py"])
    # Turn 在 2026-11-16 随数据结构搬到 chat_turn_base.py
    check("Turn 仍是每轮状态的 dataclass",
          "@dataclass" in files["chat_turn_base.py"]
          and "class Turn" in files["chat_turn_base.py"])
    check("同会话串行锁仍在", "_SESSION_LOCKS.setdefault" in files["runner.py"])
    # 反例：确认这条护栏不是摆设
    fake = dict(files)
    fake["chat.py"] += "\n        self.memory_trace = []\n"
    check("护栏没失效：注入实例状态会被抓到", bool(_state_violations(fake)))


def config_checks() -> None:
    src = (CODE / "app_core" / "config.py").read_text(encoding="utf-8")
    check("总上限默认 90（不再是 45）", '"reply_deadline_seconds": 90' in src)
    check("无进展超时默认 30", '"reply_stall_seconds": 30' in src)
    check("首字预算登记进 DEFAULTS", '"first_token_budget_seconds": 10' in src)
    check("检索门控默认关闭", '"recall_gate": "off"' in src)
    ex = (CODE / "config.example.yaml").read_text(encoding="utf-8")
    check("example 与 DEFAULTS 同步",
          "reply_stall_seconds: 30" in ex and 'recall_gate: "off"' in ex)


def gate_pipeline_checks() -> None:
    import asyncio
    import json
    import tempfile
    import types
    from unittest.mock import patch

    for mod_name in ("yaml", "httpx", "numpy", "aiosqlite"):
        if mod_name not in sys.modules:
            try:
                __import__(mod_name)
            except ImportError:
                stub = types.ModuleType(mod_name)
                if mod_name == "httpx":
                    stub.AsyncClient = type("AsyncClient", (), {})
                    stub.Timeout = lambda *a, **k: None
                    stub.Limits = lambda *a, **k: None
                sys.modules[mod_name] = stub

    from app_core import chat_turn, config, paths, shadow  # noqa: E402
    from app_core.memory_port import MemoryItem  # noqa: E402

    class _DummyMem:
        def __init__(self) -> None:
            self.embed_calls: list[list[str]] = []

        async def embed(self, texts: list[str]) -> list[list[float]]:
            self.embed_calls.append(list(texts))
            return [[0.1, 0.2] for _ in texts]

    class _DummyPort:
        def __init__(self) -> None:
            self.fact_calls = 0

        async def recall(self, query, types=("fact",), limit=5, qv=None, touch=True, trace=None):
            if "fact" in types:
                self.fact_calls += 1
                return [MemoryItem(type="fact", text="记忆A", ref="d1", payload={"score": 0.9})]
            return []

    class _DummyCore(chat_turn.ChatTurnMixin):
        first_token_budget = 10.0
        reply_deadline = 90.0
        history_max_chars = 6000
        recall_query_items = 2
        max_tokens = 1000

        def __init__(self, port, mem) -> None:
            self._port = port
            self.memory = mem

        @staticmethod
        def _is_owner(session: str) -> bool:
            return session == "private:1"

        def _affect_port(self):
            return types.SimpleNamespace(
                read=lambda: {"valence": 0.0, "valence_base": 0.0, "energy": 0.7, "social": 0.5},
                observe=lambda *a, **k: None,
                write=lambda *a, **k: None,
            )

        def _memory_port(self):
            return self._port

        async def get_digest(self, session: str) -> str:
            return ""

        async def history(self, session: str, max_chars: int = 6000) -> list[dict]:
            return []

        async def interpret_intent(self, session: str, text: str, **kw):
            return None

        async def last_user_ts(self, session: str) -> float:
            return 0.0

        async def persist_inbound(self, session: str, stored: str) -> int:
            return 1

        def seg_max(self, m: dict) -> int:
            return 25

    with tempfile.TemporaryDirectory(prefix="gate-pipe-") as td:
        old_data = paths.DATA
        old_gate = shadow.GATE_LOG
        paths.DATA = Path(td)
        shadow.GATE_LOG = Path(td) / "shadow-recall-gate.jsonl"
        try:
            async def run_mode(mode: str):
                port = _DummyPort()
                mem = _DummyMem()
                c = _DummyCore(port, mem)
                with (patch.object(config, "load", return_value={"memory": {"recall_gate": mode}, "chat": {}}),
                      patch.object(chat_turn.learn, "recent_corrections", return_value=["记住规则"]),
                      patch.object(chat_turn.learn, "skill_match", return_value=[]),
                      patch.object(chat_turn.learn, "profile_fields", return_value={})):
                    turn = await c._turn_prepare("private:1", "哈哈", None, None)
                return turn, port, mem

            t_off, p_off, m_off = asyncio.run(run_mode("off"))
            check("recall_gate=off 时寒暄不跳过召回",
                  not t_off.skip_recall and p_off.fact_calls == 1 and len(m_off.embed_calls) == 1)

            t_sh, p_sh, _ = asyncio.run(run_mode("shadow"))
            rows = [json.loads(x) for x in shadow.GATE_LOG.read_text(encoding="utf-8").splitlines() if x.strip()]
            check("recall_gate=shadow 时寒暄仍召回且落盘 need=False 样本",
                  not t_sh.skip_recall and p_sh.fact_calls == 1
                  and any(r.get("need") is False and r.get("why") == "smalltalk" for r in rows),
                  str(rows))

            t_on, p_on, m_on = asyncio.run(run_mode("on"))
            check("recall_gate=on 时寒暄跳过召回但保留纠正清单",
                  t_on.skip_recall and p_on.fact_calls == 0 and m_on.embed_calls == []
                  and "记住规则" in t_on.lessons)
        finally:
            paths.DATA = old_data
            shadow.GATE_LOG = old_gate


def main() -> None:
    gate_checks()
    gate_pipeline_checks()
    prefilter_checks()
    state_checks()
    config_checks()
    if FAILS:
        print(f"\n=== {TOTAL - len(FAILS)}/{TOTAL} 通过；失败：{', '.join(FAILS)} ===")
        sys.exit(1)
    print(f"\n=== {TOTAL}/{TOTAL} 通过 ===")


if __name__ == "__main__":
    main()
