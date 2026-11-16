"""[orchestration] 摘要与水位：批量总结、digest、矛盾裁决、追赶总结。

从 chat.py 拆出来的原因：这一组只依赖「历史 + LLM + 记忆端口 + 一把全局锁」，
和人格、通道、回复编排无关；留在 ChatCore 里把入口类撑到 1400+ 行、
最长函数 165 行。Mixin 组合：ChatCore(SummaryMixin, ChatTurnMixin)。

注意：self._claim / _release / memory / complete_summary / _memory_port 仍由
ChatCore 提供（Mixin 通过 self 访问），这里只搬方法体。
"""
from __future__ import annotations

import json
import logging
import time

import aiosqlite

from app_core import (alerts, config, contradict, followup_store, insight,
                      learn, maintenance, purge, summarize, wiki)
from app_core import paths as _paths

log = logging.getLogger("qqbot.chat")

# 与 chat.py 同源：测试改了这个常量就要一起改（见 test-nonebot-offline 的重定向段）。
DB = _paths.DATA / "chat.db"
SUMMARY_EVERY = 8


class SummaryMixin:

    async def get_digest(self, session: str) -> str:
        try:
            async with aiosqlite.connect(DB, timeout=15) as db:
                cur = await db.execute(
                    "SELECT digest FROM session_digest WHERE session=?", (session,))
                row = await cur.fetchone()
            return str(row[0] or "") if row else ""
        except Exception:
            return ""

    async def _save_digest(self, session: str, text: str, last_id: int) -> None:
        """写纪要。调用方负责保证 text 已经是最终版本。"""
        async with aiosqlite.connect(DB, timeout=15) as db:
            await db.execute(
                "INSERT OR REPLACE INTO session_digest(session, digest, last_id, updated_ts)"
                " VALUES (?,?,?,?)", (session, text, last_id, time.time()))
            await db.commit()

    async def _update_digest(self, session: str, dialog: str, last_id: int) -> str:
        """单独一次调用重写纪要（兜底路径）。主路径见 _summarize_once。"""
        old = await self.get_digest(session)
        raw = await self.complete_summary(
            [{"role": "system", "content": DIGEST_SYSTEM},
             {"role": "user",
              "content": f"【旧纪要】\n{old or '（还没有）'}\n\n【新对话】\n{dialog}"}],
            max_tokens=600)
        text = (raw or "").strip()
        if len(text) < 20:
            return old
        text = " ".join(text.split())[:DIGEST_MAX]
        try:
            await self._save_digest(session, text, last_id)
        except Exception:
            return old
        return text

    async def _maybe_summarize(self, session: str) -> None:
        lock = "summary:" + session
        if not await self._claim(lock):
            return
        try:
            await self._summarize_once(session)
        finally:
            await self._release(lock)
        # 遗忘：cleanup 原来只有 /cleanup 命令能触发，记忆只增不减。
        # 每次成功总结后顺带跑一次，用锁做「每天最多一次」（ttl 21.6h，
        # 不 release——让锁自然过期，否则下一轮立刻又能抢）。
        try:
            if await self._claim("cleanup:daily", ttl=86400 * 0.9):
                r = self.memory.cleanup()
                if r.get("archived"):
                    log.info("每日清理：归档 %d 条", r.get("archived"))
                # 知识库索引：kb.index() 原来没有任何调用点，索引是某次一次性建好的
                # 快照——之后新增的 learn/*.md 永远进不了检索（2026-11-15 实测：
                # 新笔记相似度 0.566 却查不到，因为根本没入库）。增量，只处理改动过的文件。
                try:
                    n = await kb.index()
                    if n:
                        log.info("知识库索引：新增/更新 %d 块", n)
                except Exception as e:  # noqa: BLE001
                    log.debug("知识库索引失败：%s", e)
                    alerts.record("warn", "kb_index_failed", str(e)[:200])
                # 跟催做减法：expire() 原来没有调用点，60 天没结果的事不会再被
                # 标记取消——主动消息会越问越旧，像催债。
                try:
                    from app_core import followup_store as _fu
                    _r = _fu.expire()
                    if _r.get("cancelled"):
                        log.info("跟催清理：%s", _r)
                except Exception as e:  # noqa: BLE001
                    log.debug("跟催清理失败：%s", e)
                    alerts.record("warn", "followup_expire_failed", str(e)[:200])
                # wiki 归档：archive_stale 自身限量 5 页/次，所以按周跑，
                # 每天跑会把半个库在几天内下架。
                try:
                    # 月度维护：maintenance.run() 原来只被 review 读状态、从没执行过。
                    # 三件事都是「不做不会立刻坏、做了一年省几十 MB」：JSONL 截断、
                    # WAL checkpoint、VACUUM。顺带轮转备份包（purge.rotate）。
                    if await self._claim("maint:monthly", ttl=30 * 86400 * 0.9):
                        try:
                            from app_core import maintenance as _maint
                            _mr = _maint.run()
                            log.info("月度维护：%s", _mr)
                        except Exception as e:  # noqa: BLE001
                            log.debug("月度维护失败：%s", e)
                            alerts.record("warn", "maintenance_failed", str(e)[:200])
                        try:
                            from app_core import purge as _purge2
                            _purge2.rotate()
                            _purge2.prune_old_packs()
                        except Exception as e:  # noqa: BLE001
                            log.debug("备份轮转失败：%s", e)
                    if await self._claim("wiki:weekly", ttl=7 * 86400 * 0.9):
                        from app_core import wiki as _wiki
                        _w = _wiki.archive_stale()
                        if _w:
                            log.info("wiki 归档：%d 页", len(_w))
                        # 周度反思：把最近记忆压成「理解」（type=insight）。
                        # 必须有 sources，没出处的判断直接丢弃（insight.py 里校验）。
                        try:
                            from app_core import insight as _insight
                            _ins = await _insight.generate(self.memory, self)
                            if _ins:
                                log.info("周度反思：写入 %d 条", len(_ins))
                        except Exception as e:  # noqa: BLE001
                            log.debug("周度反思失败：%s", e)
                            alerts.record("warn", "insight_failed", str(e)[:200])
                except Exception as e:  # noqa: BLE001
                    log.debug("wiki 归档失败：%s", e)
                    alerts.record("warn", "wiki_archive_failed", str(e)[:200])
        except Exception as e:  # noqa: BLE001
            log.debug("每日清理失败：%s", e)
            alerts.record("warn", "daily_maintenance_failed", str(e)[:200])

    async def catch_up_summary(self) -> dict:
        """启动时补一次总结，接住「崩溃窗口」里没固化的对话。

        正常路径是每 8 轮触发，或闲置 30 分钟后由下一条消息触发。两者都要求
        **还有下一条消息**。进程如果在这中间被杀（OOM / 手动 kill / 发版重启），
        这几轮对话已经写进 chat.db，但永远等不到总结——用户以为聊过的事它记得，
        其实一条记忆都没落。

        两个门槛防止「每次重启都白烧一次模型」：
        - 至少攒了 CATCHUP_MIN_ROUNDS 轮；
        - 距上次成功总结超过 CATCHUP_MIN_IDLE 秒（正常发版重启几秒就回来了）。
        """
        session = self.private_session()
        if not session:
            return {"ran": False, "reason": "no-admin", "pending": 0}
        async with aiosqlite.connect(DB, timeout=15) as db:
            cur = await db.execute(
                "SELECT COUNT(*) FROM messages WHERE session=? AND role='user'",
                (session,))
            n = int((await cur.fetchone())[0] or 0)
            cur = await db.execute(
                "SELECT last_count, last_id, ts FROM summaries WHERE session=?",
                (session,))
            row = await cur.fetchone()
        if not row:
            return {"ran": False, "reason": "no-state", "pending": n}
        last, last_id, ts = int(row[0] or 0), int(row[1] or 0), float(row[2] or 0)
        pending = n - last
        idle = time.time() - ts
        if pending < CATCHUP_MIN_ROUNDS or idle < CATCHUP_MIN_IDLE:
            return {"ran": False, "reason": "fresh", "pending": pending}
        lock = "summary:" + session
        if not await self._claim(lock):
            return {"ran": False, "reason": "busy", "pending": pending}
        try:
            await self._summarize_once(session, force=True)
        finally:
            await self._release(lock)
        # 不能无条件报 ran=True：_summarize_once 在 rows<4 或异常时直接 return，
        # 水位根本没动，插件却打印「补了 N 轮」。按水位是否真的前进决定。
        after = last
        try:
            async with aiosqlite.connect(DB, timeout=15) as db:
                cur = await db.execute(
                    "SELECT last_count FROM summaries WHERE session=?", (session,))
                row2 = await cur.fetchone()
            after = int((row2 or [0])[0] or 0)
        except Exception:  # noqa: BLE001
            pass
        advanced = after > last
        return {"ran": advanced,
                "reason": "caught-up" if advanced else "no-progress",
                "pending": max(0, n - after)}

    async def _check_contradictions(self, doc_id: str, data: dict,
                                    vec: list[float]) -> None:
        """新记忆写完后检查：有没有旧记忆被它推翻。

        为什么值得多一次调用：两条互相矛盾的事实同时留在库里，召回会把它们一起
        塞进 prompt，模型只能猜哪条对——表现出来就是「它记混了」。
        只在相似度够高时问（见 contradict.CAND_MIN_SCORE），大部分总结不触发。
        """
        cfg = (config.load().get("memory") or {})
        mode = str(cfg.get("contradiction_check", "on")).strip().lower()
        if mode == "off" or not vec:
            return
        try:
            cands = self.memory.similar_active(
                vec, contradict.MAX_CAND, contradict.CAND_MIN_SCORE,
                exclude={doc_id})
        except Exception as e:  # noqa: BLE001
            log.debug("矛盾检测取候选失败：%s", e)
            return
        if not cands:
            return
        # 确定性预筛：时序 / 水位 / 完全相同的文本交给 Python 判，只把
        # 「语义上是否冲突」留给模型。被筛掉的也落一行，事后能查
        # 「这次为什么没问模型」，而不是静默跳过。
        try:
            _new_row = self.memory._docs_by_ids([doc_id]).get(doc_id) or {}
            _new_meta = _new_row.get("meta") or {}
        except Exception:  # noqa: BLE001
            _new_meta = {}
        cands, dropped = contradict.prefilter(
            cands, data.get("summary") or "", _new_meta)
        for d in dropped:
            contradict.record(
                {"action": "keep", "why": "prefilter:" + d["reason"]},
                {"doc_id": d["doc_id"], "score": d["score"], "text": d["text"]},
                False, "prefilter-" + d["reason"], new_doc=doc_id, mode=mode)
        if not cands:
            return
        raw = await self.complete_summary(
            contradict.build_messages(data.get("summary") or "",
                                      data.get("key_facts") or [], cands),
            max_tokens=300, temperature=0.0)
        pairs = contradict.parse(raw)
        if not pairs:
            return
        by_id = {c["doc_id"]: c for c in cands}
        applied = 0
        for p in pairs:
            cand = by_id.get(p.get("old"))
            if cand is None:
                continue            # 模型编了个不存在的编号，忽略
            ok, reason = contradict.should_supersede(p, cand["score"], applied)
            contradict.record(p, cand, ok and mode == "on", reason,
                              new_doc=doc_id, mode=mode)
            if ok and mode == "on":
                if self.memory.supersede(cand["doc_id"], doc_id,
                                         p.get("why") or ""):
                    applied += 1
        if applied:
            log.info("矛盾检测：%d 条旧记忆被新记忆 %s 取代", applied, doc_id[:8])

    async def _summarize_once(self, session: str, force: bool = False) -> None:
        """按消息 id 水位增量固化记忆。

        用 id 而不是消息条数当水位：条数水位在工具调用、分段回复、文件消息
        这些「条数不对称」的场景下会错位，导致内容被重复总结或整段跳过。

        三个边界：
        - 写记忆成功、推水位失败 → 重跑时 range_key 幂等，不会写第二条
        - 推水位成功、写记忆失败 → 不会发生（先写后推，写失败就 return）
        - 同一段反复失败 → 熔断跳过并报错，不让它把后续记忆全堵死
        """
        async with aiosqlite.connect(DB, timeout=15) as db:
            cur = await db.execute(
                "SELECT COUNT(*), COALESCE(MAX(id),0) FROM messages "
                "WHERE session=? AND role='user'", (session,))
            n, max_id = await cur.fetchone()
            cur = await db.execute(
                "SELECT last_count, last_id, ts, fail_count, fail_id "
                "FROM summaries WHERE session=?", (session,))
            row = await cur.fetchone()
        if row:
            last, last_id, last_ts = int(row[0] or 0), int(row[1] or 0), float(row[2] or 0)
            fails, fail_id = int(row[3] or 0), int(row[4] or 0)
        else:
            last, last_id, last_ts, fails, fail_id = 0, 0, 0.0, 0, 0
        n, max_id = int(n), int(max_id)
        if row and last_id == 0 and last > 0:
            # 老库迁移：有旧的条数水位但没有 id 水位，对齐到当前最新消息，
            # 避免把已经固化过的历史再总结一遍。
            await self._save_summary_state(session, last, max_id)
            last_id = max_id
        enough_rounds = (n - last) >= SUMMARY_EVERY
        idle_long = (time.time() - last_ts) >= self.summary_idle_seconds
        if not (force or enough_rounds or idle_long):
            return
        async with aiosqlite.connect(DB, timeout=15) as db:
            cur = await db.execute(
                "SELECT id, role, content FROM messages "
                "WHERE session=? AND id>? ORDER BY id LIMIT ?",
                (session, last_id, SUMMARY_BATCH))
            rows = await cur.fetchall()
        if len(rows) < 4:
            return
        target = int(rows[-1][0])
        dialog = summarize.format_dialog([(r[1], r[2]) for r in rows])
        # 必须用 summary 模型（deepseek-chat）：flash 在长 JSON 上约 1/3 会截断，
        # 截断后 parse 不出 summary，这段对话就静默丢失了。
        # 纪要一起要：同一次调用顺带产出「旧纪要 + 这段对话」的新纪要。
        # 之前是两次独立调用、同一段对话发两遍（7 天里 summary 的输出 token
        # 和聊天本身差不多），合并后少一次完整 prompt。
        old_digest = ""
        try:
            old_digest = await self.get_digest(session)
        except Exception:
            pass
        # 已有主题词表：让模型复用标签，而不是每篇自己造一个近义词
        # （2026-11-05 实测：410 个标签里 393 个只出现过一次，实体连边因此几乎连不起来）。
        # 取不到就不加提示，摘要照常跑。
        try:
            topic_vocab = self.memory.topic_vocab()
        except Exception:  # noqa: BLE001
            topic_vocab = []
        hint = summarize.vocab_hint(topic_vocab)
        sys_prompt = summarize.SYSTEM + ("\n\n" + hint if hint else "")
        raw = await self.complete_summary(
            [{"role": "system", "content": sys_prompt},
             {"role": "user", "content": (
                 f"【旧纪要】\n{old_digest or '（还没有）'}\n\n"
                 f"【这段对话】\n\n{dialog}\n\n整理成一条长期记忆，并更新纪要。")}],
            max_tokens=1600, json_mode=True)
        data = summarize.parse(raw)
        # 归并到已有词表：模型偶尔还是会给「求职面试」这种自造的近义标签，
        # 而基于实体的连边只认完全相同的字符串。
        if data.get("topics"):
            data["topics"] = vocab_mod.canonicalize(data["topics"], topic_vocab)
        if not data.get("summary"):
            # 计数基准必须是**不移动的 last_id**：target 随每条新消息后移，
            # 用它当代号会让 fail_id 永远对不上，熔断永远数不到 SUMMARY_MAX_FAILS。
            same = (fails + 1) if fail_id == last_id else 1
            if same >= SUMMARY_MAX_FAILS:
                log.error("总结连续失败 %d 次，跳过 %s 到 id=%s 这一段（会丢这段记忆）",
                          same, session, target)
                await self._save_summary_state(session, n, target)
            else:
                await self._save_fail(session, last, last_id, same, last_id)
            return
        # 整段对话「解决了没有」：比逐条余弦更全局，折算进情绪
        if data.get("progress"):
            mood.apply_progress(data["progress"])
        try:
            vec = (await self.memory.embed([data["summary"]]))[0]
            payload = {
                "session": session,
                "topics": data.get("topics", []),
                "key_facts": data.get("key_facts", []),
                "vec": vec,
                "followups": data.get("followups", []),
                "range_key": f"{session}:{last_id}-{target}",
            }
            port = self._memory_port()
            if port is not None:
                from app_core.memory_port import MemoryItem

                doc_id = await port.remember(MemoryItem(
                    type="fact", text=data["summary"],
                    confidence=float(data.get("importance", 0.5) or 0.5),
                    source="summary", payload=payload))
            else:
                doc_id = self.memory.add_document(
                    text=data["summary"], session=session,
                    importance=data.get("importance", 0.5),
                    topics=data.get("topics", []),
                    key_facts=data.get("key_facts", []),
                    vec=vec,
                    followups=data.get("followups", []),
                    range_key=f"{session}:{last_id}-{target}")
            # 矛盾检测：新记忆可能让旧记忆过时（「在找工作」→「已入职」）。
            # 只在相似度够高时才多问一次模型；失败不影响写入。
            try:
                await self._check_contradictions(doc_id, data, vec)
            except Exception as e:  # noqa: BLE001
                log.warning("矛盾检测失败：%s", e)
            # followup 单独存一份：记忆会被清理归档，跟进事项不该跟着消失
            if data.get("followups"):
                port_t = self._memory_port()
                if port_t is not None:
                    from app_core.memory_port import MemoryItem

                    for _t in data["followups"]:
                        try:
                            await port_t.remember(MemoryItem(
                                type="task", text=str(_t), source="memory",
                                payload={"kind": "followup", "source": "memory",
                                         "ref": doc_id}))
                        except Exception as e:  # noqa: BLE001
                            log.debug("跟进写入跳过（%s）：%s", type(e).__name__, _t)
                else:
                    followup.add_many(data["followups"], "memory", doc_id)
            # 滚动纪要：历史原文只留最近 2500 字，更早的靠这段纪要接住。
            # 优先用同一次调用带回来的 digest；模型没给（或太短）再退回单独一次调用，
            # 保证旧行为可用，不会因为一次 JSON 缺字段就把纪要停更。
            try:
                new_digest = (data.get("digest") or "").strip()
                if len(new_digest) >= 20:
                    await self._save_digest(session, new_digest[:DIGEST_MAX], target)
                else:
                    await self._update_digest(session, dialog, target)
            except Exception as e:  # noqa: BLE001
                log.warning("更新滚动纪要失败：%s", e)
        except Exception as e:  # noqa: BLE001
            log.warning("写记忆失败，水位不动：%s", e)
            # 这条必须进 alerts：它是「记忆停止写入」的唯一信号，
            # 2026-11-15 凌晨欠费就是在这里静默了一整晚。
            alerts.record("error", "memory_write_failed", str(e)[:200], session)
            # 熔断必须和「解析失败」走同一个出口：只记数不判定，
            # 毒批次（例如 embedding 维度变了）会每轮重跑一次完整总结。
            same = (fails + 1) if fail_id == last_id else 1
            if same >= SUMMARY_MAX_FAILS:
                log.error("写记忆连续失败 %d 次，跳过 %s 到 id=%s 这一段（会丢这段记忆）",
                          same, session, target)
                await self._save_summary_state(session, n, target)
            else:
                await self._save_fail(session, last, last_id, same, last_id)
            return
        await self._save_summary_state(session, n, target)

    async def _save_summary_state(self, session: str, count: int, last_id: int) -> None:
        async with aiosqlite.connect(DB, timeout=15) as db:
            await db.execute(
                "INSERT OR REPLACE INTO summaries"
                "(session, last_count, last_id, ts, fail_count, fail_id) "
                "VALUES (?,?,?,?,0,0)", (session, count, last_id, time.time()))
            await db.commit()

    async def _save_fail(self, session: str, count: int, last_id: int,
                         fails: int, fail_id: int) -> None:
        """只记失败次数，不动水位。ts 保持不变，下一轮还会再试。"""
        async with aiosqlite.connect(DB, timeout=15) as db:
            await db.execute(
                "UPDATE summaries SET fail_count=?, fail_id=? WHERE session=?",
                (fails, fail_id, session))
            await db.execute(
                "INSERT OR IGNORE INTO summaries"
                "(session, last_count, last_id, ts, fail_count, fail_id) "
                "VALUES (?,?,?,?,?,?)",
                (session, count, last_id, 0.0, fails, fail_id))
            await db.commit()
