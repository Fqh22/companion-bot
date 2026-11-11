"""记忆契约（app_core.memory_port）的 P1 测试：只读、结构一致、写入未接。

不联网；用 FakeCore 代替真实记忆库。
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

CODE = Path(os.environ.get("QQBOT_CODE_DIR", "/opt/nonebot")).resolve()
sys.path.insert(0, str(CODE))
os.environ.setdefault("QQBOT_ROOT", tempfile.mkdtemp(prefix="mp-test-"))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

from app_core.memory_port import CoreMemoryPort, MemoryItem  # noqa: E402

RESULTS: list[bool] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    RESULTS.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + name + (f" | {extra}" if extra else ""))


class FakeMemory:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def recall(self, query, top_k=5, qv=None, touch=True):
        return [
            {"doc_id": "d1", "text": "他喜欢下雨天", "age_days": 3.0,
             "meta": {"create_time": 100.0, "last_access_time": 200.0, "importance": 0.8}},
            {"doc_id": "d2", "text": "他在找工作", "age_days": 1.0,
             "meta": {"create_time": 300.0, "last_access_time": 400.0, "importance": 0.9}},
        ][:top_k]

    def add_document(self, text, session, importance, topics, key_facts,
                     atoms=None, vec=None, followups=None, range_key=""):
        self.calls.append({
            "text": text, "session": session, "importance": importance,
            "topics": topics, "key_facts": key_facts, "vec": vec,
            "followups": followups, "range_key": range_key,
        })
        return "doc-new"


class FakeCore:
    def __init__(self) -> None:
        self.memory = FakeMemory()


async def main() -> None:
    port = CoreMemoryPort(FakeCore())
    items = await port.recall("工作", ("fact",), 5)
    check("返回两条", len(items) == 2, str(len(items)))
    check("类型都是 fact", all(i.type == "fact" for i in items))
    check("ref 对齐 doc_id", [i.ref for i in items] == ["d1", "d2"], str([i.ref for i in items]))
    check("text 保留", items[0].text == "他喜欢下雨天")
    check("age_days 保留", items[0].age_days == 3.0, str(items[0].age_days))
    check("created/last_used 保留",
          items[0].created == 100.0 and items[0].last_used == 200.0)
    check("confidence 取 importance", items[1].confidence == 0.9)

    trace = port.explain()
    check("explain 记录了读取", bool(trace) and trace[0]["type"] == "fact", str(trace))

    rows = [{"doc_id": i.ref, "text": i.text, "age_days": i.age_days} for i in items]
    check("转换回旧结构一致",
          rows[0]["doc_id"] == "d1" and rows[1]["text"] == "他在找工作")

    # P2：fact 写入走契约，参数与旧调用一致
    core = FakeCore()
    port = CoreMemoryPort(core)
    payload = {"session": "private:1", "topics": ["工作"], "key_facts": ["在找工作"],
               "vec": [0.1, 0.2], "followups": ["问进度"], "range_key": "private:1:1-9"}
    doc = await port.remember(MemoryItem(type="fact", text="他在找工作",
                                         confidence=0.8, payload=payload))
    check("remember 返回 doc_id", doc == "doc-new", str(doc))
    call = core.memory.calls[-1]
    check("session / importance 映射正确",
          call["session"] == "private:1" and call["importance"] == 0.8, str(call)[:80])
    check("topics / key_facts / vec / range_key 映射正确",
          call["topics"] == ["工作"] and call["key_facts"] == ["在找工作"]
          and call["vec"] == [0.1, 0.2] and call["range_key"] == "private:1:1-9")
    check("followups 映射正确", call["followups"] == ["问进度"])

    # 一致性：旧调用与契约调用产生的参数完全相同
    core2 = FakeCore()
    core2.memory.add_document(text="他在找工作", session="private:1", importance=0.8,
                              topics=["工作"], key_facts=["在找工作"],
                              vec=[0.1, 0.2], followups=["问进度"],
                              range_key="private:1:1-9")
    old_call = dict(core2.memory.calls[-1])
    new_call = dict(call)
    check("新旧写入参数完全一致", old_call == new_call,
          str({k: (old_call.get(k), new_call.get(k)) for k in old_call
               if old_call.get(k) != new_call.get(k)})[:80])

    try:
        await port.remember(MemoryItem(type="profile", text="x"))
        check("非 fact 类型应未实现", False)
    except NotImplementedError:
        check("非 fact 类型未实现", True)

    passed = sum(1 for x in RESULTS if x)
    print(f"\n=== {passed}/{len(RESULTS)} 通过 ===")
    sys.exit(0 if passed == len(RESULTS) else 1)


if __name__ == "__main__":
    asyncio.run(main())
