"""[orchestration] 主动消息的循环：频控、回应率、挑话题、生成、发送。

以前整段在 plugins/proactive 里，换通道就没了。现在通道只提供
`send(text) -> bool`，其余都在这里——频控落 chat.db 的 proactive_log，
重启不丢，还能算「发出去之后他回没回」。

判断层在 app_core/proactive.py（纯函数），这里只做「取数 + 调用 + 落库」。
"""
from __future__ import annotations

import asyncio
import logging
import random
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from app_core import followup, mood, paths
from app_core import proactive as decide
from app_core import proactive_sources as sources

log = logging.getLogger("qqbot.proactive")

REPLY_WINDOW = 7200            # 发出去 2 小时内他回了，算「有回应」
TOPIC_COOLDOWN_DAYS = decide.TOPIC_COOLDOWN_DAYS


class ProactiveRuntime:
    def __init__(self, core, send, admin_id: str = "",
                 db_path: Path | None = None,
                 interval: float = 1800.0, start_delay: float = 300.0) -> None:
        self.core = core
        self.send = send              # async (text: str) -> bool
        self.admin_id = str(admin_id or "")
        self.db = Path(db_path or paths.CHAT_DB)
        self.interval = float(interval)
        self.start_delay = float(start_delay)

    # ---------- 存储 ----------
    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.db, timeout=10)
        c.execute("PRAGMA journal_mode=WAL")
        return c

    def init(self) -> None:
        c = self._conn()
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

    def _one(self, sql: str, args: tuple = ()) -> float:
        try:
            c = self._conn()
            row = c.execute(sql, args).fetchone()
            c.close()
            return float(row[0] or 0) if row else 0.0
        except Exception:  # noqa: BLE001
            return 0.0

    @staticmethod
    def _day_start() -> float:
        now = datetime.now()
        return datetime(now.year, now.month, now.day).timestamp()

    def daily_count(self) -> int:
        return int(self._one("SELECT COUNT(*) FROM proactive_log WHERE ts>=?",
                             (self._day_start(),)))

    def last_sent_ts(self) -> float:
        return self._one("SELECT MAX(ts) FROM proactive_log")

    def _session(self) -> str:
        try:
            return self.core.private_session()
        except Exception:  # noqa: BLE001
            return ""

    def last_user_ts(self) -> float:
        s = self._session()
        if s:
            return self._one("SELECT MAX(ts) FROM messages WHERE role='user' AND session=?", (s,))
        return self._one("SELECT MAX(ts) FROM messages WHERE role='user'")

    def recent_topics(self, days: float = TOPIC_COOLDOWN_DAYS) -> set[str]:
        try:
            c = self._conn()
            rows = c.execute("SELECT topic_key FROM proactive_log WHERE ts>=?",
                             (time.time() - days * 86400,)).fetchall()
            c.close()
            return {r[0] for r in rows}
        except Exception:  # noqa: BLE001
            return set()

    def reply_rate(self, days: float = 7) -> float | None:
        """最近 N 天主动消息的回应率；样本不足返回 None（不据此调整上限）。"""
        try:
            c = self._conn()
            since = time.time() - days * 86400
            sent = int(c.execute("SELECT COUNT(*) FROM proactive_log WHERE ts>=?",
                                 (since,)).fetchone()[0] or 0)
            if sent < 3:
                c.close()
                return None
            s = self._session()
            if s:
                replied = c.execute(
                    "SELECT COUNT(*) FROM proactive_log p WHERE p.ts>=? AND EXISTS("
                    " SELECT 1 FROM messages m WHERE m.role='user' AND m.session=?"
                    " AND m.ts > p.ts AND m.ts < p.ts + ?)",
                    (since, s, REPLY_WINDOW)).fetchone()
            else:
                replied = c.execute(
                    "SELECT COUNT(*) FROM proactive_log p WHERE p.ts>=? AND EXISTS("
                    " SELECT 1 FROM messages m WHERE m.role='user'"
                    " AND m.ts > p.ts AND m.ts < p.ts + ?)",
                    (since, REPLY_WINDOW)).fetchone()
            c.close()
            return float(replied[0] or 0) / sent
        except Exception:  # noqa: BLE001
            return None

    def log_send(self, topic_key: str, topic: str, text: str) -> None:
        try:
            c = self._conn()
            with c:
                c.execute(
                    "INSERT INTO proactive_log(ts, topic_key, topic, text) VALUES (?,?,?,?)",
                    (time.time(), topic_key, topic, text))
            c.close()
        except Exception as e:  # noqa: BLE001
            log.warning("主动消息落库失败：%s", e)

    # ---------- 生成 ----------
    async def compose(self, topic: str, why: str, mood_hint: str = "") -> str:
        now = datetime.now()
        system = (
            "你在私聊里主动跟主人提一句，像突然想起来那样。"
            "15-45 字，一句话为主；不要像提醒事项，不要用「提醒你」「别忘了」这种句式；"
            "可以带一点自己的判断或关心，按你平时的人设说话。"
            f"【现在】{now:%Y-%m-%d %H:%M}，{decide.period(now.hour)}。"
            "措辞要跟时间对得上：晚上/深夜别问「睡醒了没」，早上别问「还没睡」；"
            "不用「早安/晚安」这种模板。"
            "【时间措辞】只有为什么里明确写了「今天/刚刚」才说「刚刚/你刚说」；"
            "其余一律用「之前」。超过 3 天的事不要提具体时间，更不要假装是刚发生的。"
        )
        if mood_hint:
            system += f"\n你此刻的状态：{mood_hint}。语气和长度要跟着这个状态走。"
        prompt = f"你想提的事：{topic}\n为什么现在提：{why}\n写一句你现在想说的话。"
        try:
            # 主动消息必须关思考：思考会先吃掉 100+ 输出预算，正文只剩残句。
            msg = await self.core.complete_raw(
                [{"role": "system", "content": system},
                 {"role": "user", "content": prompt}],
                max_tokens=300, thinking=False, purpose="proactive")
            return (msg.get("content") or "").strip()
        except Exception:  # noqa: BLE001
            return ""

    # ---------- 一轮 ----------
    async def tick(self) -> None:
        self.init()
        now = datetime.now()
        try:
            m = mood.settle(mood.load())
        except Exception:  # noqa: BLE001
            m = {"valence": 0.0, "energy": 0.7}

        cap = decide.daily_cap(self.reply_rate())
        ok, why = decide.gate(
            now.hour,
            time.time() - self.last_sent_ts(),
            time.time() - self.last_user_ts(),
            self.daily_count(), cap, m,
            [random.random(), random.random(), random.random()],
            threads=len(followup.due_for_asking(5)))
        log.info("[proactive] 评估：今日 %d/%d，%s", self.daily_count(), cap, why)
        if not ok:
            return

        try:
            fu, cu, me = sources.collect(lambda: self.core.memory_forgotten(3))
        except Exception:  # noqa: BLE001
            return
        picked = decide.pick_topic(fu, cu, me, self.recent_topics(),
                                   random.random(), random.random())
        if not picked:
            return
        key, topic, why = picked
        hint = mood.describe(m) if m else ""
        text = await self.compose(topic, why, hint)
        if not text or len(text) < 6:
            log.warning("[proactive] 生成的句子太短，跳过（不消费话题）：%s", topic[:30])
            return
        if not await self.send(text):
            log.error("[proactive] 发送失败（不记「问过了」）：%s", text[:40])
            return
        self.log_send(key, topic, text)
        if key.startswith("fu:"):
            try:
                followup.mark_asked([int(key[3:])])
            except Exception as e:  # noqa: BLE001
                log.warning("标记跟进已问失败：%s", e)
        log.info("[proactive] 已发送（key=%s）: %r", key, text[:60])

    async def run(self) -> None:
        await asyncio.sleep(self.start_delay)
        while True:
            try:
                await self.tick()
            except Exception as e:  # noqa: BLE001
                log.warning("[proactive] tick 失败: %s: %s", type(e).__name__, e)
            await asyncio.sleep(self.interval)
