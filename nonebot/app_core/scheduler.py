"""定时任务：日记、复盘、间隔重复、周报、知识库增量、记忆清理、提醒。

两条原则：

1. **幂等**：每个任务按「任务名 + 日期」抢一次执行权，记在 chat.db 的
   job_runs 表里。内存字典挡不住重启——23:45 重启会把日记重写一遍，
   03:40 重启会重跑复盘再烧一次 token。
2. **隔离**：每个任务单独 try。上一版是一条直线，04:00 的间隔重复抛异常，
   同一分钟的清理和整点索引就都被吞掉了。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from datetime import datetime, timedelta

import aiosqlite

from app_core import (config, diary, files, followup, kb, learn, mood, purge,
                      remind, watch, web, wiki)

NL = chr(10)
CHAT_DB = "/opt/nonebot/data/chat.db"
CLEANUP_LOG = "/opt/nonebot/data/memory-cleanup.log"

# 定时任务表：(名字, 时, 分, 补跑窗口分钟)。
# 为什么要有窗口：主循环是「跑一轮 → sleep 60s」，任务本身也会拖时间，
# 所以某一分钟完全可能被跳过。原来用 `hm == "04:30"` 精确匹配，
# 跳过一次就等于当天不跑，而且**没有任何日志**——线上 followup-expire
# 就一天都没有记录。改成「到点之后、窗口之内，只要今天还没跑就补上」。
JOBS = (
    ("diary", 23, 45, 180),
    ("mood-snapshot", 23, 55, 180),
    ("review", 3, 40, 240),
    ("spaced", 4, 0, 240),
    ("raw-archive", 4, 5, 240),
    ("cleanup", 4, 30, 240),
    ("followup-expire", 4, 30, 240),
    ("job-audit", 9, 0, 120),
    ("agent-inbox", 4, 45, 240),
)

log = logging.getLogger("qqbot.scheduler")

# 带硬信息的提醒（截止、面试、会议…）用清晰格式，日常小事用人格化说法。
# 故意只收「正式/有时点约束」的词，不收「明天」「下午」这类纯时间词，
# 否则「明天提醒我买牛奶」也会被套上闹钟模板。
HARD_HINTS = (
    "截止", "交稿", "交作业", "提交", "面试", "考试", "会议", "开会",
    "航班", "车次", "医院", "挂号", "缴费", "付款", "报到", "报名",
    "签约", "复诊", "签合同", "开庭",
)


class Scheduler:
    def __init__(self, core, send=None) -> None:
        self.core = core
        self.send = send
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._boot())

    async def _boot(self) -> None:
        """先把 job_runs 建好，免得第一个整点之前查不到表。"""
        try:
            await self._ensure_schema()
        except Exception:
            log.exception("建定时任务表失败")
        try:
            n = watch.seed((config.load().get("watch") or {}).get("topics") or [])
            if n:
                log.info("话题巡检：按配置初始化了 %d 个话题", n)
        except Exception:
            log.exception("初始化话题表失败")
        await self._loop()

    async def _ensure_schema(self) -> None:
        async with aiosqlite.connect(CHAT_DB) as db:
            await db.execute(
                "CREATE TABLE IF NOT EXISTS job_runs ("
                "name TEXT NOT NULL, day TEXT NOT NULL, ts REAL NOT NULL,"
                "PRIMARY KEY(name, day))")
            await db.commit()

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    # ---------- 执行权与日志 ----------
    async def _claim_job(self, name: str, day: str) -> bool:
        """「今天这件事还没跑过」才返回 True。重启不会重跑。"""
        try:
            async with aiosqlite.connect(CHAT_DB) as db:
                cur = await db.execute(
                    "INSERT OR IGNORE INTO job_runs(name, day, ts) VALUES (?,?,?)",
                    (name, day, time.time()))
                await db.commit()
                return cur.rowcount > 0
        except Exception:
            log.exception("抢任务执行权失败：%s", name)
            return False

    async def _safe(self, name: str, coro):
        """跑任务，异常不冒泡。**返回值原样透传**：发送类任务靠它决定要不要撤回标记。"""
        try:
            return await coro
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("定时任务 %s 失败：%s", name, e)
            return None

    async def _unclaim(self, name: str, day: str) -> None:
        """撤回「今天已经跑过」的标记。

        为什么需要：周报/巡检/知识库周检都是「先抢占、再发送」，
        发送失败时如果不撤回，这一天就算跑过了——用户只会觉得
        「这周怎么没消息」，而 job_runs 里一切正常。
        """
        try:
            async with aiosqlite.connect(CHAT_DB) as db:
                await db.execute("DELETE FROM job_runs WHERE name=? AND day=?",
                                 (name, day))
                await db.commit()
            log.info("已撤回 %s（%s）的执行标记，等下一分钟重试", name, day)
        except Exception as e:  # noqa: BLE001
            log.warning("撤回任务标记失败（%s/%s）：%s", name, day, e)

    def _log_cleanup(self, r: dict) -> None:
        """清理审计：留到哪条被归档、为什么。只记数量没法事后解释。"""
        try:
            rec = {"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                   "archived": r.get("archived"), "alive": r.get("alive"),
                   "items": (r.get("details") or [])[:50]}
            with open(CLEANUP_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + NL)
        except Exception:
            pass

    # ---------- 任务 ----------
    async def _reminder_text(self, txt: str) -> str:
        """方案 3：硬信息用清晰格式，日常小事用人格化说法。

        理由：Important 的事（面试、截止）被一句俏皮话盖住就会误事；
        而「记得买牛奶」用闹钟模板又太生硬。
        """
        t = (txt or "").strip()
        if not t:
            return ""
        if any(h in t for h in HARD_HINTS):
            return f"⏰ {t}"
        try:
            msg = await self.core.complete_raw(
                [{"role": "system", "content": (
                    "你在提醒主人一件小事。一句自然的话，10-25 字；"
                    "不要用「提醒你」「别忘了」「记得」开头；像朋友顺口提一句。")},
                 {"role": "user", "content": f"要提的事：{t}"}],
                max_tokens=120, thinking=False, purpose="reminder")
            out = (msg.get("content") or "").strip()
            if len(out) >= 6:
                return out
        except Exception:
            pass
        return f"⏰ {t}"

    async def _run_reminders(self) -> None:
        """先抢占再发送。发不出去就把抢占还回去，下一分钟重试。"""
        if not self.send:
            return
        for r in remind.due_now():
            if not remind.claim(r["id"]):
                continue
            text = await self._reminder_text(r["text"])
            if text and await self.send(text):
                log.info("提醒 %s 已发送：%s", r["id"], text[:40])
                continue
            remind.release(r["id"])

    async def _run_cleanup(self) -> None:
        self._log_cleanup(self.core.memory.cleanup())
        # prompt 快照只留 7 天：排障用，不是档案
        try:
            import aiosqlite

            from app_core.chat import DB as _chat_db
            async with aiosqlite.connect(_chat_db) as db:
                await db.execute("DELETE FROM prompt_log WHERE ts < ?",
                                 (time.time() - 7 * 86400,))
                await db.commit()
        except Exception as e:  # noqa: BLE001
            log.debug("prompt_log 清理失败：%s", e)

    async def _reindex(self) -> None:
        """向量索引 + 知识目录一起刷新：先切块入库，再给新页面写标题和摘要。"""
        try:
            await kb.index()
        except Exception as e:  # noqa: BLE001
            log.warning("知识库索引失败：%s", e)
        try:
            await wiki.refresh(self.core)
        except Exception as e:  # noqa: BLE001
            log.warning("知识目录刷新失败：%s", e)

    async def _run_followup_expire(self) -> None:
        r = await asyncio.to_thread(followup.expire)
        if r.get("cancelled"):
            log.info("待办清理：取消 %d 条（太老 %d / 问太多 %d / 积压 %d）",
                     r["cancelled"], r["old"], r["asked"], r["overflow"])

    async def _run_purge(self) -> None:
        """每月一次：物理删除已废弃很久的知识页 + 轮转备份快照。

        只清知识库。记忆库（livingmemory.db / learn/raw 对话原文）一行不碰——
        那是用户 2026-11-03 明确划的边界。
        """
        r: dict = {}
        try:
            r = await asyncio.to_thread(purge.purge_knowledge, 180, 20)
        except Exception as e:  # noqa: BLE001
            log.warning("知识页物理删除失败：%s", e)
        try:
            gone = await asyncio.to_thread(purge.rotate, ("code-", "config-", "persona-"), 10)
            await asyncio.to_thread(purge.prune_old_packs, 2)
            if gone:
                log.info("备份轮转：删掉 %d 个旧快照", len(gone))
        except Exception as e:  # noqa: BLE001
            log.warning("备份轮转失败：%s", e)
        # 月度维护：截断影子日志 + WAL checkpoint + VACUUM。
        # 放在这里是因为「每月 1 号 04:50」本来就醒了，而且备份刚做完、负载最低。
        try:
            from app_core import maintenance

            m = await asyncio.to_thread(maintenance.run)
            log.info("月度维护：日志 %d 个（收掉 %d 行，%d KB），库 %s",
                     m["logs"]["files"], m["logs"]["dropped"],
                     m["logs"]["freed_kb"],
                     "、".join("%s %d→%d KB" % (
                         Path(d["db"]).name, d["before"] // 1024, d["after"] // 1024)
                         for d in m["dbs"]))
        except Exception as e:  # noqa: BLE001
            log.warning("月度维护失败：%s", e)
        if r.get("deleted"):
            log.info("本月清理：知识页 %d 个（包 %s）", r["deleted"], r.get("tarball"))
            if self.send:
                await self.send(
                    "🧹 本月清理：删掉 %d 个废弃知识页%s\n（已打包，需要可以捞回来）"
                    % (r["deleted"],
                       "：" + "、".join(f"《{t}》" for t in r.get("titles") or [])
                       if r.get("titles") else ""))

    async def _run_weekly(self) -> bool:
        """返回是否已经送到。False 会被 _tick 撤回标记，下一分钟重试。"""
        text = await learn.weekly(self.core)
        if not text or not self.send:
            return True
        if await self.send(text):
            return True
        log.error("周报发送失败（内容已落盘 weekly 文件），撤回标记等重试")
        return False

    async def _run_watch(self) -> None:
        """话题巡检：只报真的变了的，没变化一个字不说。

        和 web_search 的区别是「主动」：那个等他问，这个不问也看。
        每周一次、最多 8 个话题，成本可以忽略；但如果没变化还发一句
        「这周没什么变化」，两周之后就会被无视——所以宁可沉默。
        """
        changes = await watch.check_all(web.search, self.core.complete_summary)
        if not changes:
            log.info("话题巡检：无变化")
            return True
        log.info("话题巡检：%d 条变化", len(changes))
        if not self.send:
            return True
        if await self.send("有变化：\n" + "\n".join(f"- {c}" for c in changes)):
            return True
        log.error("话题巡检有 %d 条变化但没发出去，撤回标记等重试", len(changes))
        return False

    async def _run_kb_audit(self, day: str = "") -> None:
        """知识库周检：死页 / 超期未验证 / 超长页 / 重复 / 溢出 + 抽两页请人校对。

        只报告不自动处理：自动归档或自动改写都会让「知识库到底有什么」变得不可预测，
        而这一整套治理的前提就是「人能看见」。

        day：要检查缺跑的目标日期，空串表示今天。
        """
        day = day or datetime.now().strftime("%Y-%m-%d")
        # 先归档再体检：归档掉的页不该紧接着出现在本周的「死页」清单里。
        # 只归档很久没人读、且（过期或从未验证）的页；文件保留，/kb_verify 可恢复。
        archived: list[dict] = []
        try:
            archived = await asyncio.to_thread(wiki.archive_stale, 5)
            if archived:
                log.info("知识库自动归档 %d 页：%s", len(archived),
                         "、".join((p.get("title") or "")[:12] for p in archived))
        except Exception as e:  # noqa: BLE001
            log.warning("知识库自动归档失败：%s", e)
        text = wiki.weekly_text(2)
        if not text:
            return True
        if archived:
            text += ("\n\n🗄 自动归档 %d 页（很久没人读 + 过期或未验证）：%s\n"
                     "文件都还在，/kb_verify 标题 可以恢复"
                     % (len(archived),
                        "、".join(f"《{(p.get('title') or '')[:12]}》" for p in archived)))
        # P2：缺跑检查并进周检，不每天打扰
        # day 由调用方传入：之前这里直接用了 _tick 的局部变量 day，
        # NameError 被下面的 except 吞掉，这段检查从上线起就没执行过。
        try:
            missing = await self.missing_jobs(day)
            if missing:
                text += "\n\n⏱ 定时任务缺跑：" + "、".join(missing)
        except Exception as e:  # noqa: BLE001
            log.warning("周检里的缺跑检查失败：%s", e)
        log.info("知识库周检：%d 字，发送中", len(text))
        if not self.send:
            return True
        if await self.send(text):
            return True
        log.error("知识库周检没发出去，撤回标记等重试")
        return False

    async def _run_mood_snapshot(self) -> None:
        """存一行当天情绪。

        参数（τ、冲量、连发系数）现在是靠推理定的，没有历史就没法回答
        「上周三它为什么一直冷」，也没法拿「我那天确实心情差」当外部标签校准。
        """
        try:
            row = mood.snapshot()
            log.info("情绪快照 %s：心情 %+.2f 精力 %.2f 互动 %.2f（消息 %d）",
                     row["day"], row["valence"], row["energy"], row["social"], row["msgs"])
        except Exception:
            log.exception("情绪快照失败")

    async def _run_raw_archive(self) -> None:
        """归档昨天的对话原文。写盘是阻塞 IO，丢线程里做。"""
        msg = await asyncio.to_thread(learn.archive_raw)
        log.info("对话原文归档：%s", msg)

    async def _loop(self) -> None:
        while True:
            try:
                await self._tick()
            except Exception:
                log.exception("tick 失败")
            await asyncio.sleep(60)

    async def _tick(self) -> None:
        now = datetime.now()
        day = now.strftime("%Y-%m-%d")

        await self._safe("reminders", self._run_reminders())

        for name, hh, mm, window in JOBS:
            due, day_key, late = self._due(now, hh, mm, window)
            if not due:
                continue
            if not await self._claim_job(name, day_key):
                continue
            if late >= 1:
                log.info("定时任务 %s 补跑（晚 %d 分钟，归属 %s）", name, late, day_key)
            else:
                log.info("定时任务 %s 开始（%s）", name, day_key)
            await self._safe(name, self._run_job(name, day_key))

        # 每月 1 号 04:50：物理清理（知识库废弃页 + 备份轮转）。记忆库不碰。
        if now.day == 1:
            due, day_key, late = self._due(now, 4, 50, 24 * 60)
            if due and await self._claim_job("purge", day_key):
                await self._safe("purge", self._run_purge())

        # 知识库索引：每小时第一次 tick 就跑一次。原来要求 minute == 0，
        # 被跳过一分钟就少索引一小时；现在只要有空就补上。
        if await self._claim_job("kb-%02d" % now.hour, day):
            asyncio.create_task(self._reindex())

        # 周任务按「目标日期是不是周日」判定，而不是「现在是不是周日」。
        # 旧写法是 if now.weekday()==6，周日 21:00 停机、周一凌晨才恢复时，
        # now 已经是周一 → 这周的周报和知识库周检直接整周丢掉。
        # 交给 _due 的归属日期 + 24 小时窗口，就能像其他任务一样补跑。
        weekly_window = 24 * 60
        # 周日 20:30 话题巡检：先看外面变了什么，再写周报（21:00）
        due, day_key, late = self._due(now, 20, 30, weekly_window)
        if due and self._is_sunday(day_key) and await self._claim_job("watch", day_key):
            if await self._safe("watch", self._run_watch()) is False:
                await self._unclaim("watch", day_key)
        due, day_key, late = self._due(now, 21, 0, weekly_window)
        if due and self._is_sunday(day_key) and await self._claim_job("weekly", day_key):
            if await self._safe("weekly", self._run_weekly()) is False:
                await self._unclaim("weekly", day_key)
        # 周报之后 10 分钟再做知识库周检：先看这一周发生了什么，再看知识还准不准
        due, day_key, late = self._due(now, 21, 10, weekly_window)
        if due and self._is_sunday(day_key) and await self._claim_job("kb-audit", day_key):
            if await self._safe("kb-audit", self._run_kb_audit(day_key)) is False:
                await self._unclaim("kb-audit", day_key)

    @staticmethod
    def _is_sunday(day_key: str) -> bool:
        """日任务归属的那天是不是周日。周报/周检据此判定，不依赖「现在几点」。"""
        try:
            return datetime.strptime(day_key, "%Y-%m-%d").weekday() == 6
        except Exception:
            return False

    @staticmethod
    def _due(now: datetime, hh: int, mm: int, window_min: float) -> tuple[bool, str, int]:
        """现在是否该跑这个任务。返回 (该不该跑, 归属日期, 晚了多少分钟)。

        归属日期用「目标时刻所在的日期」：23:45 的任务在 00:30 补跑时，
        归属仍是前一天——否则日记会写成今天的（空的）。
        """
        target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if target > now:
            target -= timedelta(days=1)
        late = int((now - target).total_seconds() // 60)
        return 0 <= late <= window_min, target.strftime("%Y-%m-%d"), late

    async def _run_job(self, name: str, day: str) -> None:
        """名字 → 具体任务。集中在一处，加任务时不会漏注册。"""
        if name == "diary":
            await diary.write_day(self.core, day)
        elif name == "mood-snapshot":
            await self._run_mood_snapshot()
        elif name == "review":
            # 复盘失败/跳过以前被直接丢掉：纠正清单和画像可以连续几周不更新，
            # 聊天里还在犯同样的错，而日志里一条异常都没有。
            res = await learn.review(self.core)
            if "失败" in str(res):
                log.error("复盘失败：%s", res)
                return False
            log.info("复盘：%s", res)
        elif name == "spaced":
            await learn.spaced_review(self.core)
        elif name == "raw-archive":
            await self._run_raw_archive()
        elif name == "cleanup":
            await self._run_cleanup()
        elif name == "followup-expire":
            await self._run_followup_expire()
        elif name == "job-audit":
            await self._run_job_audit(day)
        elif name == "agent-inbox":
            await self._run_agent_inbox()

    async def _run_agent_inbox(self) -> None:
        """清掉 agentd 收件箱里超过 24 小时的文件。

        收文件时只记「最近一个」，没人删文件本身；一天就能攒四个，
        而 /opt/agentd 和 bot 的数据在同一个盘上。
        """
        try:
            res = files.cleanup_inbox(24)
            if res["removed"]:
                log.info("agent 收件箱清理：%d 个文件，释放 %d KB",
                         res["removed"], res["freed"] // 1024)
        except Exception as e:  # noqa: BLE001
            log.warning("agent 收件箱清理失败：%s", e)

    async def _run_job_audit(self, day: str) -> None:
        """检查前一天的任务有没有缺跑。

        静默跳过是这套调度最阴的失败模式：当天一切正常，只是某件事没做。
        每天 09:00 只写日志（不打扰），周日周检里再汇总发给管理员。
        """
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        missing = await self.missing_jobs(yesterday)
        if missing:
            log.error("定时任务缺跑（%s）：%s", yesterday, "、".join(missing))
        else:
            log.info("定时任务齐全（%s）", yesterday)

    async def missing_jobs(self, day: str) -> list[str]:
        """某天哪些任务没有记录。

        两个排除条件，否则新装机/刚部署的那天会满屏误报：
        - 任务目标时刻早于「第一次有任务记录」的时间 → 那时还没跑起来；
        - 目标时刻还没到 → 今天晚些的任务不该算缺。
        kb 是每小时一次，按「实际在跑的时长」折算期望次数。
        """
        expected = [(n, hh, mm) for n, hh, mm, _w in JOBS if n != "job-audit"]
        try:
            async with aiosqlite.connect(CHAT_DB) as db:
                cur = await db.execute("SELECT name FROM job_runs WHERE day=?", (day,))
                had = {r[0] for r in await cur.fetchall()}
                cur = await db.execute("SELECT MIN(ts) FROM job_runs")
                row = await cur.fetchone()
                first_ts = float(row[0] or 0)
        except Exception:
            return []
        now = datetime.now()
        try:
            day_at = datetime.strptime(day, "%Y-%m-%d")
        except Exception:
            return []
        started = max(day_at, datetime.fromtimestamp(first_ts)) if first_ts else day_at
        missing = []
        for name, hh, mm in expected:
            target = day_at.replace(hour=hh, minute=mm)
            if target < started or target > now:
                continue
            if name not in had:
                missing.append(name)
        span_h = (min(day_at + timedelta(days=1), now) - started).total_seconds() / 3600
        expect_kb = max(0, int(span_h))
        kb = sum(1 for n in had if n.startswith("kb-"))
        if kb + 1 < expect_kb:
            missing.append("kb(%d/%d)" % (kb, expect_kb))
        return missing
