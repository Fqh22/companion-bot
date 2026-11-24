"""投递账本契约。普通和主动投递使用同一份 SQLite 历史与引用映射。"""
from __future__ import annotations

import sqlite3
from contextlib import closing

from harness import bootstrap, check, report, temp_root

bootstrap()
from app_core import followup_store as ledger  # noqa: E402
from app_core.proactive_runtime import ProactiveRuntime  # noqa: E402


def main() -> int:
    db = temp_root("delivery-ledger-") / "chat.db"
    ledger.DB = db
    with closing(sqlite3.connect(db)) as c, c:
        c.execute("CREATE TABLE messages(id INTEGER PRIMARY KEY, session TEXT, role TEXT, content TEXT, ts REAL)")
    ledger.init()
    ProactiveRuntime(None, None, db_path=db).init()
    item = "预约更换卧室窗帘"
    ledger.add(item)
    fid = ledger.id_for_text(item)
    session = "private:delivery-test"
    question = "卧室窗帘预约了吗？"
    refs = (("qq:test-delivery-ref", question),)

    message = ledger.record_delivery(db, session, question, track_followup=True, references=refs)
    retry = ledger.record_delivery(db, session, question, track_followup=True, references=refs)
    check("普通投递重复回执只写一次", message == retry)
    state = ledger.answer_turn("还没。", session, reply_to=refs[0][0])
    check("普通消息按平台引用识别跟进事项", state.get("touched") == [fid], state)
    foreign = ledger.answer_turn("搞定了", "private:other", reply_to=refs[0][0])
    check("其他会话不能使用这条送达引用", not foreign, foreign)

    topic_key = f"fu:{fid}"
    push_refs = (("feishu:test-delivery-push", question),)
    push = ledger.record_delivery(db, session, question, track_followup=True, references=push_refs,
                                  topic_key=topic_key, topic=item)
    retry = ledger.record_delivery(db, session, question, track_followup=True, references=push_refs,
                                   topic_key=topic_key, topic=item)
    check("主动投递重复回执只结算一次", push == retry)
    with closing(sqlite3.connect(db)) as c, c:
        row = c.execute(
            "SELECT p.text,m.content,f.ask_count,p.platform_ref FROM proactive_log p "
            "JOIN messages m ON m.id=p.assistant_msg_id "
            "JOIN followups f ON p.topic_key='fu:' || f.id WHERE p.platform_ref=?",
            (push_refs[0][0],)).fetchone()
        check("主动投递共用历史、回执和跟进入账",
              row == (question, question, 2, push_refs[0][0]), row)
        c.execute(
            "CREATE TRIGGER reject_test_delivery BEFORE INSERT ON proactive_log "
            "BEGIN SELECT RAISE(ABORT,'test write failure'); END")
    try:
        ledger.record_delivery(db, session, "必须回滚", track_followup=True,
                               references=(("qq:reject", "必须回滚"),),
                               topic_key=topic_key, topic=item)
    except sqlite3.IntegrityError:
        pass
    with closing(sqlite3.connect(db)) as c:
        counts = tuple(c.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
                       for name in ("messages", "delivery_refs", "proactive_log"))
    check("账本失败整体回滚，历史和引用不留下半成品", counts == (2, 2, 1), counts)
    with closing(sqlite3.connect(db)) as c, c:
        c.execute("DELETE FROM messages WHERE id=?", (message,))
        remaining = c.execute(
            "SELECT COUNT(*) FROM delivery_refs WHERE assistant_msg_id=?", (message,)).fetchone()[0]
    check("删除历史消息同时清理引用数据", remaining == 0)
    return report()


if __name__ == "__main__":
    raise SystemExit(main())
