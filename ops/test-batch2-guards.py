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


def main() -> None:
    gate_checks()
    prefilter_checks()
    state_checks()
    config_checks()
    if FAILS:
        print(f"\n=== {TOTAL - len(FAILS)}/{TOTAL} 通过；失败：{', '.join(FAILS)} ===")
        sys.exit(1)
    print(f"\n=== {TOTAL}/{TOTAL} 通过 ===")


if __name__ == "__main__":
    main()
