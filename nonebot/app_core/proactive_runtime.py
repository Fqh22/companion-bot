"""[orchestration] 主动消息的循环：频控、回应率、挑话题、生成、发送。

以前整段在 plugins/proactive 里，换通道就没了。现在通道只提供
`send(text) -> bool | Receipt`，其余都在这里——频控落 chat.db 的 proactive_log，
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

from app_core import followup, mood, paths, perception, proactive_channels
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
        self.send = send              # async (text: str) -> bool | Receipt
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
            # 逐次结算：每一条发送自己记「有没有被回应」，不再靠"最新一条 + 内存
            # 去重"。旧版本重启后会把同一次发送重算一遍，同一话题发两次也只算一次。
            _cols = {str(r[1]) for r in c.execute(
                "PRAGMA table_info(proactive_log)").fetchall()}
            _upgrading = "settled_ts" not in _cols
            for ddl in (
                "ALTER TABLE proactive_log ADD COLUMN settled_ts REAL NOT NULL DEFAULT 0",
                "ALTER TABLE proactive_log ADD COLUMN ignored INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE proactive_log ADD COLUMN session TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE proactive_log ADD COLUMN assistant_msg_id INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE proactive_log ADD COLUMN answered_ts REAL NOT NULL DEFAULT 0",
                "ALTER TABLE proactive_log ADD COLUMN platform_ref TEXT NOT NULL DEFAULT ''",
            ):
                try:
                    c.execute(ddl)
                except Exception:  # noqa: BLE001  列已存在
                    pass
            if _upgrading:
                # 只迁移升级前就已经存在的行：旧逻辑已经结算过其中最新一条，
                # 补记会把历史全部算成"没理"。升级之后新写入的行保持 0，
                # 即使进程重启也会正常结算（不会被这条迁移吞掉）。
                c.execute("UPDATE proactive_log SET settled_ts=ts")
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

    def _inbound_since(self, ts: float) -> bool:
        """生成期间有没有新的入站。消息可能还没落库，看入站标记。"""
        try:
            from app_core import inflight
            if inflight.inbound_after(self._session(), ts):
                return True
        except Exception:  # noqa: BLE001
            pass
        return self.last_user_ts() > ts + 0.05

    def last_user_ts(self) -> float:
        """主人最近一条消息的时间；没有可信主人会话时返回 0。

        不能回退成「所有用户的最新消息」：那会把陌生人当活跃信号，
        让频控和回应率被无关会话带偏。
        """
        s = self._session()
        if not s:
            return 0.0
        return self._one(
            "SELECT MAX(ts) FROM messages WHERE role='user' AND session=?", (s,))

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
        session = self._session()
        if not session:
            return None
        now = time.time()
        try:
            c = self._conn()
            try:
                total, replied = c.execute(
                    "SELECT COUNT(*), SUM(answered_ts>0 AND answered_ts<=ts+?) "
                    "FROM proactive_log WHERE session=? AND topic_key LIKE 'fu:%' "
                    "AND ts>=? AND settled_ts>0",
                    (REPLY_WINDOW, session, now - days * 86400)).fetchone()
            finally:
                c.close()
            return float(replied or 0) / total if total >= 3 else None
        except Exception:  # noqa: BLE001
            return None

    async def _deliver(self, key: str, topic: str, text: str) -> bool:
        receipt = await self.send(text)
        if not receipt:
            return False
        ref = receipt.reference if isinstance(receipt, proactive_channels.Receipt) else ""
        refs = ((ref, text),) if ref else ()
        await self.core.record_delivery(
            self._session(), text, references=refs, topic_key=key, topic=topic)
        return True

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
    def _settle_ignored(self) -> None:
        """逐条结算：每条超过 REPLY_WINDOW 的发送，各自判断有没有被回应。

        两次没理的跟进会被 mark_ignored 转成 postponed，不再主动问。
        旧实现只看"最新一条 + 内存里的 key"：同一话题发两次只记一次没理，
        进程重启后内存清空，同一次发送又被重算。现在状态落在 proactive_log
        的 settled_ts 上，同一条只结算一次，重启不重计。
        """
        from app_core import followup as fu_mod

        session = self._session()
        if not session:
            # 没有可信主人会话：无法判断"他回没回"，也不能拿所有用户的消息
            # 顶替。保留未结算状态，等身份配好后再算。
            log.info("[proactive] 没有可信主人会话，跳过未回应结算")
            return
        try:
            fu_mod.init()
        except Exception as e:  # noqa: BLE001
            log.debug("跟进表初始化失败：%s", e)
            return
        now = time.time()
        cutoff = now - REPLY_WINDOW
        try:
            c = self._conn()
        except Exception as e:  # noqa: BLE001
            log.debug("读取主动消息记录失败：%s", e)
            return
        try:
            rows = c.execute(
                "SELECT id, topic_key, ts, text, session FROM proactive_log "
                "WHERE settled_ts=0 AND ts<=? AND session IN (?, '') ORDER BY ts, id",
                (cutoff, session)).fetchall()
            for pid, key, sent, question, recorded_session in rows:
                try:
                    fid = int(key[3:]) if key.startswith("fu:") and key[3:].isdigit() else None
                    if fid is None:
                        with c:
                            c.execute("UPDATE proactive_log SET settled_ts=? WHERE id=?", (now, pid))
                        continue
                    with c:
                        # 新发送只结算已经归到这一次提问的回复。
                        # 旧数据只在聊天历史能还原紧邻问答时补记，避免随机私聊算回应。
                        legacy_answer = 0.0
                        if not recorded_session:
                            legacy_answer = self._legacy_answer(c, session, question, sent, fid)
                        cur = c.execute(
                            "UPDATE proactive_log SET settled_ts=?, "
                            "answered_ts=MAX(answered_ts, ?), "
                            "ignored=CASE WHEN MAX(answered_ts, ?) BETWEEN ts AND ts+? "
                            "AND MAX(answered_ts, ?)>0 THEN 0 ELSE 1 END "
                            "WHERE id=? AND settled_ts=0",
                            (now, legacy_answer, legacy_answer, REPLY_WINDOW, legacy_answer, pid))
                        if not cur.rowcount:
                            continue
                        ignored, answered = c.execute(
                            "SELECT ignored, answered_ts FROM proactive_log WHERE id=?", (pid,)
                        ).fetchone()
                        if ignored and not answered:
                            fu_mod.mark_ignored([fid], conn=c)
                        elif legacy_answer:
                            c.execute(
                                "UPDATE followups SET answered_ts=MAX(answered_ts, ?) WHERE id=?",
                                (legacy_answer, fid))
                except Exception as e:  # noqa: BLE001
                    log.debug("结算 %s 失败：%s", key, e)
        finally:
            c.close()

    @staticmethod
    def _legacy_answer(c: sqlite3.Connection, session: str, question: str,
                       sent: float, fid: int) -> float:
        from app_core.followup_domain import responds_to

        asked = c.execute(
            "SELECT id FROM messages WHERE session=? AND role='assistant' "
            "AND content=? AND ts BETWEEN ? AND ? ORDER BY id DESC LIMIT 1",
            (session, question, sent - 10, sent + 1)).fetchone()
        if not asked:
            return 0.0
        reply = c.execute(
            "SELECT role, content, ts FROM messages WHERE session=? AND id>? "
            "ORDER BY id LIMIT 1", (session, asked[0])).fetchone()
        item = c.execute("SELECT text FROM followups WHERE id=?", (fid,)).fetchone()
        if (item and reply and reply[0] == "user" and sent <= reply[2] <= sent + REPLY_WINDOW
                and responds_to(item[0], question, reply[1])):
            return float(reply[2])
        return 0.0

    async def tick(self) -> None:
        self.init()
        if not self._session():
            # 连主人都没有可信会话时，不评估、不统计、不发；否则会把
            # 「所有用户的消息」当成他的活跃信号。
            log.info("[proactive] 没有可信主人会话，跳过本轮")
            return
        try:
            self._settle_ignored()
        except Exception as e:  # noqa: BLE001
            log.debug("未回应检查失败：%s", e)
        now = datetime.now()
        try:
            m = mood.settle(mood.load())
        except Exception:  # noqa: BLE001
            m = {"valence": 0.0, "energy": 0.7}

        cap = decide.daily_cap(self.reply_rate())
        # 天气突变：世界给的开口理由，比 followup 更「在场」。同样受时段/频率约束，
        # 一周最多一次；他今天没提过天气/出门，就不打扰。
        _wc = None
        try:
            if perception.mentioned_today(self._session()) and perception.alert_allowed(now):
                _ch = await perception.change()
                if _ch:
                    _wc = ("weather:" + now.strftime("%Y-%m-%d"),
                           f"今天比昨天降温 {abs(_ch['delta']):.0f} 度，他今天提过要出门",
                           "天气突变")
        except Exception as e:  # noqa: BLE001
            log.debug("天气突变检查失败：%s", e)
        ok, why = decide.gate(
            now.hour,
            time.time() - self.last_sent_ts(),
            time.time() - self.last_user_ts(),
            self.daily_count(), cap, m,
            [random.random(), random.random(), random.random()],
            threads=1 if _wc else len(followup.due_for_asking(5)))
        log.info("[proactive] 评估：今日 %d/%d，%s", self.daily_count(), cap, why)
        if not ok:
            return

        if _wc:
            key, topic, why2 = _wc
            compose_at = time.time()
            text = await self.compose(topic, why2, mood.describe(m) if m else "")
            if self._inbound_since(compose_at):
                log.info("[proactive] 生成期间他发了新消息，取消天气提醒")
                return
            if text and len(text) >= 6 and await self._deliver(key, topic, text):
                perception.mark_alert(now)
                log.info("[proactive] 天气突变已发送（key=%s）：%r", key, text[:60])
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
        compose_at = time.time()
        text = await self.compose(topic, why, hint)
        if not text or len(text) < 6:
            log.warning("[proactive] 生成的句子太短，跳过（不消费话题）：%s", topic[:30])
            return
        if self._inbound_since(compose_at):
            log.info("[proactive] 生成期间他发了新消息，取消这次发送")
            return
        if key.startswith("fu:"):
            try:
                if not followup.still_due(int(key[3:])):
                    log.info("[proactive] 事项已不再可问，取消发送：%s", key)
                    return
            except Exception as e:  # noqa: BLE001
                log.debug("发送前复查跟进失败：%s", e)
        if not await self._deliver(key, topic, text):
            log.error("主动消息发送失败，不登记提问")
            return
        log.info("[proactive] 已发送（key=%s）: %r", key, text[:60])

    async def run(self) -> None:
        await asyncio.sleep(self.start_delay)
        while True:
            try:
                await self.tick()
            except Exception as e:  # noqa: BLE001
                log.warning("[proactive] tick 失败: %s: %s", type(e).__name__, e)
            await asyncio.sleep(self.interval)
