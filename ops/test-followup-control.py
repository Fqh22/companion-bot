"""主人跟进控制契约，从 ChatCore.reply 到数据库和主动候选。"""
from __future__ import annotations

import asyncio
import sqlite3
import time
from contextlib import closing
from datetime import timedelta
from unittest.mock import patch

from harness import bootstrap, check, report, temp_root

bootstrap()
from companion_core import EngineConfig, create_application  # noqa: E402


async def main() -> int:
    root = temp_root("followup-control-")
    app = create_application(EngineConfig(root))
    await app.start()
    core = app.core
    core.admin_uid = "owner"
    session = "private:owner"
    db = root / "nonebot/data/chat.db"
    from app_core import followup_store as fu, timenorm
    from app_core.chat_turn_base import Turn
    from app_core.followup_domain import quiet_control

    async def no_fetch(*args):
        raise AssertionError("跟进控制不应调用模型或检索")

    core._prepare_fetches = no_fetch

    def state(item):
        with closing(sqlite3.connect(db)) as c:
            return c.execute(
                "SELECT status,done,quiet_until,ignored FROM followups WHERE text=?", (item,)
            ).fetchone()

    async def ask(text, **kw):
        chunks, images = await core.reply(session, text, **kw)
        check("控制回复直接生成 " + text, len(chunks) == 1 and not images)
        return chunks[0]

    now = time.time()
    local = timenorm.from_ts(now)
    until = (local.replace(hour=0, minute=0, second=0, microsecond=0)
             + timedelta(days=7 - local.weekday())).timestamp()
    rule = quiet_control("这周先别问", now)
    check("本周暂停到下周一零点", rule and rule.until == until, rule)
    check("带逗号的等待指令命中", quiet_control("这件事，等我自己提", now).action == "hold")
    for text in ("他说不用跟进了", "不用跟进了吗？", "如果不用跟进了怎么办", "怎么回答别人说不用跟进了"):
        check("转述和疑问不改变状态 " + text, quiet_control(text, now) is None)

    rent = "向管家确认短租方案"
    bill = "地铁卡续费"
    refund = "核对押金退款"
    for item in (rent, bill, refund):
        check("建立测试事项 " + item, fu.add(item))

    await ask("地铁卡续费3天先别问")
    due_at = state(bill)[2]
    check("定时暂停打开闸门前两条入口都跳过",
          bill not in [r["text"] for r in fu.due_for_asking(50)]
          and bill not in [r["text"] for r in fu.pending(50)] and not fu.for_prompt(bill))
    with patch.object(fu.time, "time", return_value=due_at + 1):
        check("到期恢复候选资格", bill in [r["text"] for r in fu.due_for_asking(50)])

    question = "短租方案问过了吗？"
    ref = "qq:test-short-rent"
    await core.record_delivery(session, question, references=((ref, question),))
    result = await ask("等我自己提", reply_to=ref)
    check("引用只暂停对应事项", rent in result and state(rent)[:3] == ("todo", 0, -1)
          and state(refund)[:3] == ("todo", 0, 0))
    fid = fu.id_for_text(rent)
    fu.mark_ignored([fid, fid])
    check("暂停期间结算不会增加忽略次数", state(rent)[3] == 0)
    visitor = Turn(session="private:visitor", user_text="恢复跟进短租", owner=False,
                   images=[], store_text=None, rid="visitor", t0=now)
    core._prepare_followup(visitor, visitor.user_text)
    check("非主人无法解除暂停", state(rent)[2] == -1 and not visitor.followup_control_reply)
    related = Turn(session=session, user_text="短租方案我先看看", owner=True,
                  images=[], store_text=None, rid="owner", t0=now)
    core._prepare_followup(related, related.user_text)
    rules = [text for _, text in related.prefetch.get("followup", [])]
    check("本人聊到暂停事项时提醒普通回复别追问", any("别主动问进展" in rule for rule in rules))
    check("普通回应不自动解除等待", state(rent)[2] == -1)
    await ask("恢复跟进短租")
    check("明确恢复只解除暂停", state(rent)[:3] == ("todo", 0, 0))

    clarification = await ask("不用跟进了")
    check("无对象只问一次且保留事项", clarification.startswith("你指哪件事")
          and state(refund)[:3] == ("todo", 0, 0))
    await ask("押金退款")
    check("下一句选中事项后结束，其他事项保留", state(refund)[:3] == ("cancelled", 1, -1)
          and state(rent)[:2] == ("todo", 0))
    check("结束后从普通待办和主动候选移除",
          not fu.for_prompt(refund) and refund not in [r["text"] for r in fu.due_for_asking(50)])
    check("区分取消跟进与任务完成", refund in fu.quiet_topics())

    await ask("等我自己提")
    unknown_id = await core.persist_inbound(session, "不知道")
    check("对象仍不清楚时不循环确认", fu.control_turn("不知道", session, unknown_id) is None)
    next_id = await core.persist_inbound(session, rent)
    check("一次澄清已消费，后续普通消息不执行旧指令",
          fu.control_turn(rent, session, next_id) is None and state(rent)[2] == 0)

    old = root / "old.db"
    with closing(sqlite3.connect(old)) as c, c:
        c.execute(
            "CREATE TABLE followups (id INTEGER PRIMARY KEY, text TEXT NOT NULL, "
            "source TEXT NOT NULL DEFAULT 'memory', ref_id TEXT, created_ts REAL NOT NULL, "
            "last_asked_ts REAL DEFAULT 0, ask_count INTEGER DEFAULT 0, done INTEGER DEFAULT 0)")
        c.execute("INSERT INTO followups(text, created_ts, ask_count) VALUES(?,?,?)", ("旧事项", now, 1))
    with patch.object(fu, "DB", old):
        fu.init()
    with closing(sqlite3.connect(old)) as c:
        migrated = c.execute("SELECT text,ask_count,quiet_until FROM followups").fetchone()
    check("旧库升级保留事项和已问次数", migrated == ("旧事项", 1, 0), migrated)

    await app.stop()
    return report()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
