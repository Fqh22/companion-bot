"""proactive 插件：主动消息。

频控**不靠内存计数**——进程重启一次就清零，等于没有上限。所有发送记录落在
chat.db 的 proactive_log 里，重启不丢，还能算出「发出去之后他回没回」。
回应率低就自动少说话：这是最影响体验的部分，宁少勿多。
"""
from __future__ import annotations

import asyncio
import hashlib
import random
import sqlite3
import time
from datetime import datetime

from nonebot import get_bots, get_driver, logger

from app_core import paths as _paths
from app_core import followup, learn, mood
from app_core import proactive as decide
from plugins.chat import core

CHAT_DB = _paths.DATA / "chat.db"
driver = get_driver()

REPLY_WINDOW = 7200            # 发出去 2 小时内他回了，算「有回应」
TOPIC_COOLDOWN_DAYS = decide.TOPIC_COOLDOWN_DAYS


def _admin_id() -> int:
    """管理员 QQ 由 config.yaml 的 admin.user_id 决定，未配置时返回 0。"""
    try:
        return int(core.admin_uid)
    except (TypeError, ValueError):
        return 0


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(CHAT_DB, timeout=10)
    c.execute("PRAGMA journal_mode=WAL")
    return c


def _init() -> None:
    c = _conn()
    with c:
        c.execute(
            "CREATE TABLE IF NOT EXISTS proactive_log ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "ts REAL NOT NULL,"
            "topic_key TEXT NOT NULL,"
            "topic TEXT NOT NULL,"
            "text TEXT NOT NULL)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_proactive_ts ON proactive_log(ts)")
    c.close()


def _one(sql: str, args: tuple = ()) -> float:
    try:
        c = _conn()
        row = c.execute(sql, args).fetchone()
        c.close()
        return float(row[0] or 0) if row else 0.0
    except Exception:
        return 0.0


def _day_start() -> float:
    now = datetime.now()
    return datetime(now.year, now.month, now.day).timestamp()


def _daily_count() -> int:
    return int(_one("SELECT COUNT(*) FROM proactive_log WHERE ts>=?", (_day_start(),)))


def _last_sent_ts() -> float:
    return _one("SELECT MAX(ts) FROM proactive_log")


def _session() -> str:
    """主人会话。陌生人的私聊也在 messages 里，不隔离会左右主动消息的闸门。"""
    try:
        return core.private_session()
    except Exception:  # noqa: BLE001
        return ""


def _last_user_ts() -> float:
    s = _session()
    if s:
        return _one("SELECT MAX(ts) FROM messages WHERE role='user' AND session=?", (s,))
    return _one("SELECT MAX(ts) FROM messages WHERE role='user'")


def _recent_topics(days: float = TOPIC_COOLDOWN_DAYS) -> set[str]:
    try:
        c = _conn()
        rows = c.execute("SELECT topic_key FROM proactive_log WHERE ts>=?",
                         (time.time() - days * 86400,)).fetchall()
        c.close()
        return {r[0] for r in rows}
    except Exception:
        return set()


def _reply_rate(days: float = 7) -> float | None:
    """最近 N 天主动消息的回应率：发出去 2 小时内他有没有说话。

    没有足够样本时返回 None（不据此调整上限）。
    """
    try:
        c = _conn()
        since = time.time() - days * 86400
        rows = c.execute("SELECT COUNT(*) FROM proactive_log WHERE ts>=?",
                         (since,)).fetchone()
        sent = int(rows[0] or 0)
        if sent < 3:
            c.close()
            return None
        s = _session()
        if s:
            replied = c.execute(
                "SELECT COUNT(*) FROM proactive_log p WHERE p.ts>=? AND EXISTS("
                " SELECT 1 FROM messages m WHERE m.role='user' AND m.session=?"
                " AND m.ts > p.ts AND m.ts < p.ts + ?)", (since, s, REPLY_WINDOW)).fetchone()
        else:
            replied = c.execute(
                "SELECT COUNT(*) FROM proactive_log p WHERE p.ts>=? AND EXISTS("
                " SELECT 1 FROM messages m WHERE m.role='user'"
                " AND m.ts > p.ts AND m.ts < p.ts + ?)", (since, REPLY_WINDOW)).fetchone()
        c.close()
        return float(replied[0] or 0) / sent
    except Exception:
        return None


def _daily_cap(rate: float | None) -> int:
    """口径在 app_core.proactive，这里只留个薄封装。"""
    return decide.daily_cap(rate)


def _log_send(topic_key: str, topic: str, text: str) -> None:
    try:
        c = _conn()
        with c:
            c.execute(
                "INSERT INTO proactive_log(ts, topic_key, topic, text) VALUES (?,?,?,?)",
                (time.time(), topic_key, topic, text))
        c.close()
    except Exception:
        pass


async def _compose(topic: str, why: str, mood_hint: str = "") -> str:
    system = (
        "你在私聊里主动跟主人提一句，像突然想起来那样。"
        "15-45 字，一句话为主；不要像提醒事项，不要用「提醒你」「别忘了」这种句式；"
        "可以带一点自己的判断或关心，按你平时的人设说话。"
    )
    if mood_hint:
        system += f"\n你此刻的状态：{mood_hint}。语气和长度要跟着这个状态走。"
    prompt = f"你想提的事：{topic}\n为什么现在提：{why}\n写一句你现在想说的话。"
    try:
        # 主动消息必须关思考：思考会先吃掉 100+ 输出预算，
        # 之前 max_tokens=120 时正文只剩残句（线上真的发出过「对了，」）。
        msg = await core.complete_raw(
            [{"role": "system", "content": system},
             {"role": "user", "content": prompt}],
            max_tokens=300, thinking=False, purpose="proactive")
        return (msg.get("content") or "").strip()
    except Exception:
        return ""


def _candidates() -> tuple[list, list, list]:
    """从三个存储里取候选。这里只管取数，选谁由 decide.pick_topic 决定。"""
    fu = []
    try:
        for it in followup.due_for_asking(5):
            fu.append((f"fu:{it['id']}", it["text"], "之前他说要做的事，顺口问问进展"))
    except Exception:
        pass
    cu = [("cur:" + hashlib.sha1(x.encode("utf-8")).hexdigest()[:8], x,
           "之前没讲清楚，顺口问一句")
          for x in learn.read_list(learn.CURIOUS)]
    me = []
    try:
        for r in core.memory_forgotten(3):
            me.append(("mem:" + r["doc_id"], r["text"][:120],
                       "突然想起这件事，有阵子没提了"))
    except Exception:
        pass
    return fu, cu, me


async def _open_threads() -> int:
    """他留了话头、还没下文的事有几件。宁少勿多：只数到期该问的。"""
    try:
        return len(followup.due_for_asking(5))
    except Exception:  # noqa: BLE001
        return 0


async def _pick_topic() -> tuple[str, str, str] | None:
    """挑话题：取候选 → 纯函数选。

    **不在这里记「问过了」**：以前先 mark_asked 再 compose+发送，
    生成的句子为空/太短、或者所有 bot 都发失败时，这次跟进就算被消费掉了
    （ask_count+1、冷却 5 天），用户永远等不到那句关心。改成发送成功后记。
    """
    fu, cu, me = _candidates()
    return decide.pick_topic(fu, cu, me, _recent_topics(),
                             random.random(), random.random())


async def _tick() -> None:
    _init()
    now = datetime.now()
    try:
        m = mood.settle(mood.load())
    except Exception:
        m = {"valence": 0.0, "energy": 0.7}

    sent_today = _daily_count()
    cap = _daily_cap(_reply_rate())
    ok, why = decide.gate(now.hour,
                          time.time() - _last_sent_ts(),
                          time.time() - _last_user_ts(),
                          sent_today, cap, m,
                          [random.random(), random.random(), random.random()],
                          threads=_open_threads())
    logger.info(f"[proactive] 评估：今日 {sent_today}/{cap}，{why}")
    if not ok:
        return

    picked = await _pick_topic()
    if not picked:
        return
    key, topic, why = picked
    hint = mood.describe(m) if m else ""
    text = await _compose(topic, why, hint)
    if not text or len(text) < 6:
        # 太短的多半是被截断的残句，宁可这次不发。不记「问过了」，下一轮还能再试。
        logger.warning(f"[proactive] 生成的句子太短，跳过（不消费话题）：{topic[:30]}")
        return
    uid = _admin_id()
    if not uid:
        return
    for bot in get_bots().values():
        try:
            await bot.call_api("send_private_msg", user_id=uid, message=text)
            _log_send(key, topic, text)
            # 发送成功才记「问过了」：失败不该消费这次跟进
            if key.startswith("fu:"):
                try:
                    followup.mark_asked([int(key[3:])])
                except Exception:  # noqa: BLE001
                    pass
            # 自己说过的话也要进历史，否则下一轮它完全不知道
            await core.record_assistant(f"private:{uid}", text, "proactive")
            logger.info(f"[proactive] 已发送（key={key}）: {text[:60]!r}")
            break
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[proactive] 发送到 bot {getattr(bot, 'self_id', '?')} 失败：{e}")
            continue
    else:
        logger.error(f"[proactive] 没有可用的 bot，消息没发出去（不记「问过了」）：{text[:40]}")


async def _loop() -> None:
    await asyncio.sleep(300)
    while True:
        try:
            await _tick()
        except Exception as e:  # noqa: BLE001
            # 不要静默吞：主动消息坏了最难发现（用户只会觉得「它最近不理我」）
            logger.warning(f"[proactive] tick 失败: {type(e).__name__}: {e}")
        await asyncio.sleep(1800)


@driver.on_startup
async def _start() -> None:
    try:
        _init()
    except Exception:
        pass
    asyncio.create_task(_loop())
