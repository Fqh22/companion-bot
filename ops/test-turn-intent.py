"""当轮意图契约。用假模型验证理解、归因与实际入账。"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import closing

from harness import bootstrap, check, report, temp_root

bootstrap()
from companion_core import EngineConfig, create_application  # noqa: E402


async def main() -> int:
    root = temp_root("turn-intent-")
    app = create_application(EngineConfig(root))
    await app.start()
    core = app.core
    core.admin_uid = "owner"
    session = "private:owner"
    from app_core import followup_store as fu

    calls = []
    result = {}

    async def classify(messages, **kwargs):
        calls.append((messages, kwargs))
        return {"content": json.dumps(result)}

    async def no_fetch(*args):
        raise AssertionError("控制动作应该在准备模型回复前完成")

    core.complete_raw = classify
    core._prepare_fetches = no_fetch

    for text, voice, read, draw in (
        ("朗读这段", True, True, False),
        ("我想听你亲口说", True, False, False),
        ("别用语音，打字就行", False, False, False),
        ("别画图，打字就好", False, False, False),
        ("给这段故事配个插画", False, False, True),
        ("今天忙完挺开心", False, False, False),
    ):
        plan = await core.interpret_intent(session, text)
        check("交付形式 " + text, (plan.voice, plan.read_aloud, plan.draw) == (voice, read, draw), plan)
    voice_override = await core.interpret_intent(session, "别用语音，打字就行", voice=True)
    check("语音输入也能明确要求文字", not voice_override.voice)
    check("明确措辞和普通聊天无需额外模型调用", not calls, len(calls))

    item = "向管家确认短租方案"
    fu.add(item)
    question = "短租方案问过了吗？"
    await core.record_delivery(session, question, references=(("qq:rent", question),))
    raw = "有消息我会找你"
    result.update(action="hold", target="", duration="", media="none", draw=False, evidence=raw)
    chunks, _ = await core.reply(session, raw, reply_to="qq:rent")
    check("自然说法通过相邻提问进入等待", item in chunks[0] and "等你自己提" in chunks[0], chunks)
    check("模型只收到最近对话与当轮证据", calls[-1][1]["purpose"] == "turn_intent"
          and question in calls[-1][0][-1]["content"], calls[-1][1])

    raw = "短租先放几天吧"
    result.update(action="snooze", target="短租", duration="", evidence=raw)
    chunks, _ = await core.reply(session, raw)
    check("缺少明确时间只问时间", chunks[0].startswith("暂停到哪天"), chunks)
    chunks, _ = await core.reply(session, "三天")
    check("澄清时间后写入同一个已定位事项", "前先不问" in chunks[0], chunks)
    with closing(sqlite3.connect(fu.DB)) as c:
        quiet = c.execute("SELECT quiet_until FROM followups WHERE text=?", (item,)).fetchone()[0]
    check("定时暂停已保存", quiet > 0, quiet)

    raw = "等我有消息就来找你"
    result.update(action="cancel", target="", evidence=raw)
    plan = await core.interpret_intent(session, raw, reply_to="qq:rent")
    check("等我有消息不会被当作结束", plan.control and plan.control.action == "hold", plan)
    result.update(action="cancel", target="编造的事项", evidence=raw)
    plan = await core.interpret_intent(session, raw, reply_to="qq:rent")
    check("模型不能自选或捏造事项", plan.control is None, plan)
    before = len(calls)
    plan = await core.interpret_intent(session, "她说等我有消息就来找你")
    check("转述不进入跟进操作", plan.control is None and len(calls) == before)

    migrate = "迁移脚本重新部署"
    fu.add(migrate)
    question = "迁移脚本重新部署跑通了吗？"
    await core.record_delivery(session, question, references=(("qq:migrate", question),))
    raw = "我昨晚跑通啦"
    result.update(action="completed", target="", evidence=raw)
    plan = await core.interpret_intent(session, raw)
    updated = fu.answer_turn(raw, session, report=plan.report, expected_question=plan.question)
    check("自然完成答复只更新匹配的提问",
          updated.get("done") == [fu.id_for_text(migrate)], updated)

    result.update(action="snooze", target="短租", duration="99天", evidence="短租先放几天吧")
    plan = await core.interpret_intent(session, "短租先放几天吧")
    check("模型补造的期限不落库", plan.control and plan.control.until == 0, plan)

    before = len(calls)
    plan = await core.interpret_intent(session, "我先不吃饭了")
    check("没有事项线索的普通接话不额外请求模型", plan.control is None and len(calls) == before)

    async def unavailable(*args, **kwargs):
        raise TimeoutError("offline")

    core.complete_raw = unavailable
    plan = await core.interpret_intent(session, "短租先放几天吧")
    check("模型超时退回本地规则且没有写操作", plan.control is None and not plan.draw)

    await app.stop()
    return report()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
