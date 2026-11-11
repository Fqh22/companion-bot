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

    def find_by_prefix(self, prefix: str, limit: int = 5) -> list[str]:
        return ["d1"] if prefix.startswith("d1") else []

    def delete(self, doc_id: str) -> bool:
        self.deleted = getattr(self, "deleted", []) + [doc_id]
        return doc_id == "d1"

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

    def _memory_port(self):
        return CoreMemoryPort(self)


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

    # P3：forget 真删（ref = doc_id）
    check("forget 命中返回 True", await port.forget("d1") is True)
    check("forget 未命中返回 False", await port.forget("nope") is False)
    check("forget 调到了存储删除", core.memory.deleted == ["d1", "nope"],
          str(getattr(core.memory, "deleted", [])))

    # P3：/forget 两步确认
    from app_core.introspect import forget_text

    core3 = FakeCore()
    out = await forget_text(core3, "工作")
    check("候选里带编号", "[d1" in out, out[:50])
    out2 = await forget_text(core3, "id:d123")
    check("按编号真删", "已删掉" in out2, out2[:30])
    out3 = await forget_text(core3, "id:zzzz")
    check("找不到编号时报错", "没找到" in out3, out3[:30])

    # P5：knowledge 写入走 note_write
    from app_core import learn as learn_mod

    calls = []
    orig = learn_mod.note_write

    def fake_note(title, content, tag="", project="", source="自撰", stale_days=0):
        calls.append((title, content, tag, stale_days))
        return "已记下"

    learn_mod.note_write = fake_note
    try:
        out_k = await port.remember(MemoryItem(
            type="knowledge", text="2K 的两种定义",
            payload={"title": "2K", "tag": "显示", "stale_days": 30}))
        check("knowledge 写入走 note_write",
              out_k == "已记下" and bool(calls) and calls[0][0] == "2K", str(calls)[:60])
    finally:
        learn_mod.note_write = orig

    # task 读：followup 与 reminder 都进契约
    from app_core import followup_store
    from app_core import paths as _paths_mp

    _paths_mp.DATA.mkdir(parents=True, exist_ok=True)
    followup_store.add("交房租", source="test")
    tasks = await CoreMemoryPort(FakeCore()).recall("", ("task",), 10)
    refs = [x.ref for x in tasks]
    check("task 读含 followup", any(r.startswith("followup:") for r in refs), str(refs)[:80])
    check("task 文本正确", any(x.text == "交房租" for x in tasks), str([x.text for x in tasks])[:60])

    # 契约宽度 = 实现宽度：未实现的类型必须显式报错，不能静默返回空
    for bad_type in ("mood",):
        try:
            await CoreMemoryPort(FakeCore()).recall("", (bad_type,))
            check(f"recall({bad_type}) 应报错", False)
        except NotImplementedError:
            check(f"recall({bad_type}) 未实现即报错", True)
    try:
        await CoreMemoryPort(FakeCore()).forget("d1", type="task")
        check("forget(task) 应报错", False)
    except NotImplementedError:
        check("forget(task) 未实现即报错", True)

    # P5：profile 写入（抽出的 save_profile）
    from app_core import learn as learn_mod2
    from app_core import profile as profile_mod2

    learn_mod2.LEARN.mkdir(parents=True, exist_ok=True)
    st = await port.remember_profile({"技术与项目": "在做 companion-bot"}, {"技术与项目": 0.8}, {"技术与项目": 1700000000.0}, {"技术与项目": True})
    fields_now = learn_mod2.profile_fields()
    check("profile 写入生效", fields_now.get("技术与项目") == "在做 companion-bot", str(fields_now)[:60])
    check("profile 返回 dropped 字段", "dropped" in st, str(st)[:50])

    # P4：记忆轨迹
    from app_core.introspect import trace_text
    from app_core import paths

    check("无轨迹时给提示", "还没有" in trace_text())
    line = '{"ts": 1700000000, "query": "工作", "ids": ["d1"], "trace": [{"type": "fact", "n": 1}]}'
    paths.DATA.mkdir(parents=True, exist_ok=True)
    (paths.DATA / "memory_trace.jsonl").write_text(line + "\n", encoding="utf-8")
    got = trace_text()
    check("轨迹含问句与条数", "工作" in got and "1 条" in got, got[:60])

    passed = sum(1 for x in RESULTS if x)
    print(f"\n=== {passed}/{len(RESULTS)} 通过 ===")
    sys.exit(0 if passed == len(RESULTS) else 1)


if __name__ == "__main__":
    asyncio.run(main())
