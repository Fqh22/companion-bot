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
    async def recall(self, query, top_k=5, qv=None, touch=True):
        return [
            {"doc_id": "d1", "text": "他喜欢下雨天", "age_days": 3.0,
             "meta": {"create_time": 100.0, "last_access_time": 200.0, "importance": 0.8}},
            {"doc_id": "d2", "text": "他在找工作", "age_days": 1.0,
             "meta": {"create_time": 300.0, "last_access_time": 400.0, "importance": 0.9}},
        ][:top_k]


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

    try:
        await port.remember(MemoryItem(type="fact", text="x"))
        check("remember 应未实现", False)
    except NotImplementedError:
        check("remember 未实现", True)

    passed = sum(1 for x in RESULTS if x)
    print(f"\n=== {passed}/{len(RESULTS)} 通过 ===")
    sys.exit(0 if passed == len(RESULTS) else 1)


if __name__ == "__main__":
    asyncio.run(main())
