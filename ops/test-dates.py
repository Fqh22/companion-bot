#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""日期锚点回归：历史逐条日期、总结按来源时间换算、写库不再猜「写库当天」。

为什么单独一套（2026-11-22 复查）：同一件事的日期在三条链路上各有各的锚点，
任何一处用错「今天」都会让机器人复述旧的「明天」，或换成一个错的具体日期：

1. 普通聊天历史：history() 原来只取 role/content，模型不知道旧消息是哪天说的；
2. 批量总结 / 记忆写入：add_document 原来用 datetime.now()（写库当天）当相对
   日期的基准，批量总结跨天、或崩溃后补总结时，会给旧消息补一个晚几天的日期；
3. 总结 → 跟进 → 主动消息：入库时间被当成说话时间，「几天前他说过」算错。

本套覆盖：跨午夜、跨年、逐条不同日期、延迟总结、无来源时间、原子事实 / 跟进
与正文一致、提醒授权时间不被批准时刻重算。全部用固定时间点，不依赖宿主时区。

用法：
    python ops/test-dates.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

HERE = Path(__file__).resolve().parent
CODE_DIR = os.environ.get("QQBOT_CODE_DIR") or str(HERE.parent / "nonebot")
sys.path.insert(0, CODE_DIR)
TMP = Path(tempfile.mkdtemp(prefix="qqbot-dates-"))
os.environ["QQBOT_ROOT"] = str(TMP / "root")

from app_core import memory as memory_mod          # noqa: E402
from app_core import prompt as prompt_mod          # noqa: E402
from app_core import summarize as summarize_mod    # noqa: E402
from app_core import timenorm                      # noqa: E402
from app_core import audit                         # noqa: E402
from app_core import followup as followup_mod      # noqa: E402
from app_core import followup_store                # noqa: E402
from app_core import proactive_sources             # noqa: E402
from app_core import tools as tools_mod            # noqa: E402
from app_core import history as history_mod        # noqa: E402
from app_core.scheduling import remind as remind_mod  # noqa: E402

RESULTS = []


def check(name: str, cond: bool, extra: str = "") -> None:
    RESULTS.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + name + ((" | " + str(extra)) if extra else ""))


def dt(m: int, d: int, h: int = 12, mi: int = 0) -> datetime:
    return datetime(2026, m, d, h, mi)


# ---------- A. timenorm：逐条换算、跨年、昨天/前天、不猜 ----------
def test_timenorm() -> None:
    a = timenorm.normalize("明天要做一件事", now=dt(11, 20, 21))
    b = timenorm.normalize("明天要做一件事", now=dt(11, 22, 9))
    check("同句「明天」按各自说话时间得到不同日期",
          "2026-11-21" in a and "2026-11-23" in b and a != b, f"{a} / {b}")
    check("输出带完整年份（跨年可消歧）", "2026-11-21" in a, a)
    _past = timenorm.normalize("昨天交了初稿，前天面的试", now=dt(11, 22, 9))
    check("昨天 / 前天换算成绝对日期",
          "2026-11-21" in _past and "2026-11-20" in _past, _past)
    _ny = timenorm.normalize("明天就跨年了", now=dt(12, 31, 23, 50))
    check("跨年：12-31 的明天是 2027-01-01", "2027-01-01" in _ny, _ny)
    check("没有基准时间就不猜（保留原文）",
          timenorm.normalize("明天见") == "明天见")
    check("歧义表达仍不猜", timenorm.normalize("周五见", now=dt(11, 22)) == "周五见")
    check("月底换算正确", "2026-11-30" in timenorm.normalize("月底交", now=dt(11, 5)))
    _night = timenorm.normalize("明晚八点吃饭", now=dt(11, 20, 10))
    check("明晚 → 明天日期 + 晚上", "2026-11-21" in _night and "晚上" in _night, _night)
    _big = timenorm.normalize("大后天出发", now=dt(11, 20))
    check("大后天不被后天截断", "2026-11-23" in _big and "大" not in _big, _big)
    _wk = timenorm.normalize("下周三面试", now=dt(11, 14))
    check("下周三：2026-11-14 周六 → 11-18（周三）", "2026-11-18" in _wk, _wk)


# ---------- B. 历史：库里留原话，进 prompt 的副本带日期 ----------
def _mk_chat_db() -> None:
    con = sqlite3.connect(TMP / "chat.db")
    with con:
        con.execute(
            "CREATE TABLE IF NOT EXISTS messages("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, session TEXT, role TEXT, "
            "content TEXT, ts REAL)")
    con.close()


class _Hist(history_mod.HistoryMixin):
    history_max_chars = 4000
    history_max_items = 40
    history_item_max = 800


def test_history() -> None:
    _mk_chat_db()
    history_mod.DB = TMP / "chat.db"
    ts1 = datetime(2026, 11, 20, 21, 0).timestamp()
    ts2 = datetime(2026, 11, 22, 9, 0).timestamp()
    con = sqlite3.connect(TMP / "chat.db")
    with con:
        con.execute("INSERT INTO messages(session, role, content, ts) VALUES(?,?,?,?)",
                   ("s", "user", "明天要做一件事", ts1))
        con.execute("INSERT INTO messages(session, role, content, ts) VALUES(?,?,?,?)",
                   ("s", "user", "明天要做一件事", ts2))
        last_id = con.execute("SELECT MAX(id) FROM messages").fetchone()[0]
    con.close()
    h = _Hist()
    rows = asyncio.run(h.history("s"))
    check("history() 带回每条消息的 ts",
          len(rows) == 2 and abs(rows[0]["ts"] - ts1) < 1 and abs(rows[1]["ts"] - ts2) < 1,
          str([r.get("ts") for r in rows]))
    check("history() 仍保留原话（库里不动）",
          all(r["content"] == "明天要做一件事" for r in rows))
    dated = prompt_mod.dated_history(rows)
    _d1, _d2 = dated[0]["content"], dated[1]["content"]
    check("prompt 副本两条都带自己的日期前缀",
          _d1.startswith("[2026-11-20") and _d2.startswith("[2026-11-22"), f"{_d1[:30]} / {_d2[:30]}")
    check("prompt 副本里同句「明天」换成两个不同日期",
          "2026-11-21" in _d1 and "2026-11-23" in _d2, f"{_d1} / {_d2}")
    check("没有 ts 的历史原样透传（不编日期）",
          prompt_mod.dated_history([{"role": "user", "content": "明天见"}])[0]["content"]
          == "明天见")
    check("message_ts 能按消息 id 取到落库时间",
          abs(asyncio.run(h.message_ts(last_id)) - ts2) < 1)
    msgs = prompt_mod.build_messages({}, rows, "在吗")
    _hist_msgs = [m for m in msgs if m.get("content", "").startswith("[2026-11-2")]
    check("build_messages 注入的历史是带日期的副本",
          len(_hist_msgs) == 2 and '"ts"' not in json.dumps(msgs[:1]), str(len(_hist_msgs)))


# ---------- C. 总结：一批对话逐条解析 ----------
def test_summary_dialog() -> None:
    dialog = summarize_mod.format_dialog([
        ("user", "明天要交稿", datetime(2026, 11, 20, 23, 50).timestamp()),
        ("assistant", "好", datetime(2026, 11, 20, 23, 51).timestamp()),
        ("user", "明天要面试", datetime(2026, 11, 22, 8, 0).timestamp()),
    ])
    check("跨午夜的批次里两句「明天」各按自己时间换算",
          "2026-11-21" in dialog and "2026-11-23" in dialog, dialog.replace("\n", " / "))
    check("summary 每行带真实说话时间",
          dialog.count("[2026-11-20") == 2 and "[2026-11-22" in dialog)
    legacy = summarize_mod.format_dialog([("user", "明天见")])
    check("老的两元组调用不编日期", legacy == "[他] 明天见", legacy)
    check("总结 prompt 要求写绝对日期、不写成已完成",
          "YYYY-MM-DD" in summarize_mod.SYSTEM and "不要写成已经做完" in summarize_mod.SYSTEM)


# ---------- D. 记忆写入：按来源时间换算，不拿写库当天猜 ----------
def _mem() -> "memory_mod.Memory":
    memory_mod.DB = TMP / "livingmemory.db"
    m = memory_mod.Memory()
    m.init()
    return m


def _doc(doc_id: str) -> tuple[str, dict]:
    con = sqlite3.connect(TMP / "livingmemory.db")
    row = con.execute("SELECT text, metadata FROM documents WHERE doc_id=?",
                      (doc_id,)).fetchone()
    con.close()
    return (row[0], json.loads(row[1] or "{}")) if row else ("", {})


def _atoms(doc_id: str) -> list[str]:
    con = sqlite3.connect(TMP / "livingmemory.db")
    rows = con.execute(
        "SELECT content FROM memory_atoms WHERE status='active' AND parent_memory_id="
        "(SELECT id FROM documents WHERE doc_id=?)", (doc_id,)).fetchall()
    con.close()
    return [r[0] for r in rows]


def test_memory_write() -> None:
    mem = _mem()
    ts1 = datetime(2026, 11, 20, 21, 0).timestamp()
    d1 = mem.add_document(
        text="他说 11-20 晚上说明天要做一件事", session="s", importance=0.6,
        topics=["t"], key_facts=["明天前交稿"], atoms=["明天要去医院"],
        followups=["明天问一下进度"], anchor_ts=ts1)
    text, meta = _doc(d1)
    check("正文里的「明天」按来源时间换算成 11-21",
          "2026-11-21" in text and "明天" not in text, text)
    check("原子事实与正文用同一个来源日期",
          any("2026-11-21" in x and "明天" not in x for x in _atoms(d1)), _atoms(d1))
    check("metadata 记下 source_ts（说话时间）",
          abs(float(meta.get("source_ts") or 0) - ts1) < 1
          and float(meta.get("create_time") or 0) > ts1, str(meta.get("source_ts")))
    ts2 = datetime(2026, 11, 22, 9, 0).timestamp()
    d2 = mem.add_document(
        text="他 11-22 又提了一次：明天要做一件事", session="s", importance=0.6,
        topics=["t"], key_facts=["明天要做一件事"], anchor_ts=ts2)
    text2, _ = _doc(d2)
    check("同一句「明天」在另一天写成另一个日期",
          "2026-11-23" in text2 and "2026-11-21" not in text2, text2)
    d3 = mem.add_document(text="没有来源时间：明天要做一件事", session="s",
                          importance=0.5, topics=["t"], key_facts=[])
    text3, meta3 = _doc(d3)
    check("没有来源时间就保留原文，不猜写库当天",
          "明天" in text3 and "2026-" not in text3, text3)
    check("没有来源时间不写 source_ts", float(meta3.get("source_ts") or 0) == 0.0)
    ts_ny = datetime(2026, 12, 31, 23, 50).timestamp()
    d4 = mem.add_document(text="他说跨年那晚说明天要开始", session="s",
                          importance=0.6, topics=["t"], key_facts=[], anchor_ts=ts_ny)
    text4, _ = _doc(d4)
    check("记忆里的跨年日期带年份", "2027-01-01" in text4, text4)
    # 延迟总结：入库时间晚 3 天，换算仍按来源时间
    d5 = mem.add_document(text="延迟总结：明天交稿", session="s", importance=0.6,
                          topics=["t"], key_facts=[],
                          anchor_ts=datetime(2026, 11, 20, 9).timestamp())
    text5, meta5 = _doc(d5)
    check("延迟总结不把入库日当说话日",
          "2026-11-21" in text5
          and float(meta5.get("create_time") or 0) - float(meta5.get("source_ts") or 0) > 60,
          text5)


# ---------- E. 跟进 / 主动消息：用说话时间，不拿入库时间 ----------
def test_followup_dates() -> None:
    followup_store.DB = TMP / "chat.db"
    followup_store.init()
    src = datetime(2026, 11, 19, 9, 0).timestamp()   # 距 11-22 12:00 满 3 天
    followup_store.add("2026-11-21 前交稿", "memory", "doc-x", source_ts=src)
    due = followup_store.due_for_asking(5)
    hit = [x for x in due if "前交稿" in x["text"]]
    check("跟进表记录 source_ts", bool(hit) and abs(hit[0]["source_ts"] - src) < 1,
          str(hit[0] if hit else due))
    now = datetime(2026, 11, 22, 12, 0).timestamp()
    fake = [{"id": 1, "text": "2026-11-21 前交稿", "source": "memory",
             "created": now, "asks": 0, "status": "todo", "source_ts": src}]
    real_due = followup_mod.due_for_asking
    followup_mod.due_for_asking = lambda limit=5: fake
    try:
        cand = proactive_sources.followup_candidates(5, now=now)
        check("主动消息用说话时间算「几天前」",
              bool(cand) and "约 3 天前" in cand[0][2], cand)
        fake[0]["source_ts"] = 0
        cand2 = proactive_sources.followup_candidates(5, now=now)
        check("旧记录没有来源时间就不断言日期",
              bool(cand2) and "之前" in cand2[0][2] and "天前" not in cand2[0][2], cand2)
    finally:
        followup_mod.due_for_asking = real_due


# ---------- F. 提醒授权：提交时冻结时间，批准不重算 ----------
def test_remind_freeze() -> None:
    before_midnight = datetime(2026, 11, 20, 23, 50).timestamp()
    frozen = remind_mod.freeze_when("明天九点", now=before_midnight)
    check("提交时把相对时间冻成绝对时间", frozen.startswith("2026-11-21"), frozen)
    check("绝对时间原样保留", remind_mod.freeze_when("2026-11-21 09:00") == "2026-11-21 09:00")
    check("看不懂的时间不猜", remind_mod.freeze_when("有朝一日") == "有朝一日")

    async def _propose() -> tuple[str, dict]:
        audit.PENDING_FILE = TMP / "audit-pending.json"
        audit.AUDIT_LOG = TMP / "audit.jsonl"
        audit.requires_approval = lambda name: True
        out = await tools_mod.execute(None, "remind_me",
                                      {"text": "交稿", "when": "明天九点"})
        pending = json.loads((TMP / "audit-pending.json").read_text(encoding="utf-8"))
        item = next(iter(pending.values()))
        return out, item

    out, item = asyncio.run(_propose())
    when = str((item.get("args") or {}).get("when") or "")
    check("待授权记录里存的是绝对时间（批准不会重算）",
          bool(when) and when[:4].isdigit() and "今天" not in when
          and "明天" not in when, when)
    check("授权提示里显示同一个时间", "提醒时间：" in out and when in out, out)


def main() -> int:
    test_timenorm()
    test_history()
    test_summary_dialog()
    test_memory_write()
    test_followup_dates()
    test_remind_freeze()
    total, passed = len(RESULTS), sum(RESULTS)
    print("\n=== 日期锚点回归：%d/%d 通过 ===" % (passed, total))
    if passed != total:
        print("失败 %d 项" % (total - passed))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
