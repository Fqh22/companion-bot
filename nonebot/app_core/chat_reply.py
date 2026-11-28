"""[orchestration] 回复主链路：组装 prompt → LLM → 工具循环 → 分段 → 影子/摘要调度。

从 chat.py 拆出来的原因：这一组是「一轮回复怎么产出来」的唯一入口，
和装配（__init__）、历史读写、摘要水位、人格加载分开后，改回复逻辑不必通读 900 行。
Mixin 组合：ChatCore(HistoryMixin, SummaryMixin, ReplyMixin, ChatTurnMixin)。
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time

import aiosqlite

from app_core import config, inflight, shadow, tool_loop, tools
from app_core.delivery import Capability, DeliveryPolicy, plan_delivery
from app_core import prompt as prompt_mod
from app_core.chat_turn_base import _chat
from app_core.tool_policy import RETRY_BLOCKED

log = logging.getLogger("qqbot.chat")


class ReplyMixin:

    def _owner_configured(self) -> bool:
        """有没有任何可信主人身份：QQ 号、admin.identities，或平台主人（飞书）。"""
        return bool(self.admin_uid or self.admin_ids
                    or getattr(self, "platform_owner_sessions", set()))

    def config_issues(self) -> list[str]:
        """启动自检：只查「不报错但静默失效」的配置。

        `admin.user_id` 留空或为 "0" 时，记忆不写入、主动消息和定时推送全发不出去，
        而且**不抛任何异常**——线上表现只是「它好像变笨了、不找我了」。
        这种失败模式不该靠人记住文档，所以启动时打 ERROR、/ping 里也报。
        """
        issues: list[str] = []
        if not self._owner_configured():
            issues.append(
                "admin.user_id / admin.identities 未配置：没有可信主人身份，"
                "所有会话都按普通用户处理（无私有记忆、无写工具、不发主动消息）。"
                "改 /opt/nonebot/config.yaml 后 systemctl restart nonebot")
        if not str(self.api or "").strip():
            issues.append("llm.api 为空：所有对话都会失败")
        if config.stale():
            issues.append(
                "config.yaml 改过了但进程没重载：现在跑的还是旧配置。"
                "确认改对了就 systemctl restart nonebot")
        err = config.load_error()
        if err:
            issues.append(
                f"config.yaml 解析失败（{err}）：所有配置已回落默认值，"
                "受控写操作已暂停（不能把读失败当成审批关闭）。"
                "修好后 systemctl restart nonebot")
        _ch = _chat()
        if _ch._PERSONA_ERROR:
            issues.append(
                f"人格文件异常（{_ch._PERSONA_ERROR}）：现在用空人格回复。"
                f"检查 {_ch.PERSONA} 后发 /reload_persona")
        return issues

    def config_status(self) -> str:
        """一行配置状态，给 /ping 用。只报「有没有配」，不回显 QQ 号。"""
        if not self._owner_configured():
            return "⚠ admin.user_id / admin.identities 未配置（普通对话模式：无记忆、无写工具、不主动发消息）"
        return f"管理员已配置 · 模型 {self.model} · 工具上限 {self.max_tool_rounds} 轮"

    def _sync_llm_db(self) -> None:
        """DB 路径可能被测试改过，LLM 持有一份引用，用之前同步一下。"""
        db = _chat().DB
        if self.llm.db != db:
            self.llm.db = db

    async def last_prompts(self, n: int = 1) -> list[dict]:
        """最近几条 prompt 快照（含正文与分块账）。排障用：它当时到底看到了什么。"""
        n = max(1, min(5, int(n or 1)))
        async with aiosqlite.connect(_chat().DB, timeout=15) as db:
            cur = await db.execute(
                "SELECT ts, session, chars, flags, COALESCE(body,'') AS body, "
                "COALESCE(blocks,'') AS blocks "
                "FROM prompt_log ORDER BY id DESC LIMIT ?", (n,))
            rows = await cur.fetchall()
        out = []
        for r in rows:
            try:
                blocks = json.loads(r[5]) if r[5] else []
            except Exception:  # noqa: BLE001
                blocks = []
            out.append({"ts": r[0], "session": r[1], "chars": r[2],
                        "flags": r[3] or "", "body": r[4] or "",
                        "blocks": blocks})
        return out

    async def usage_stats(self, days: int = 1) -> dict:
        since = time.time() - days * 86400
        async with aiosqlite.connect(_chat().DB, timeout=15) as db:
            cur = await db.execute(
                "SELECT COUNT(*), COALESCE(SUM(prompt_tokens),0), COALESCE(SUM(completion_tokens),0) "
                "FROM usage WHERE ts >= ?", (since,))
            row = await cur.fetchone()
            cur = await db.execute(
                "SELECT model, COUNT(*), SUM(prompt_tokens), SUM(completion_tokens), "
                "COALESCE(SUM(cache_hit),0), COALESCE(SUM(cache_miss),0) "
                "FROM usage WHERE ts >= ? GROUP BY model", (since,))
            by_model = await cur.fetchall()
            cur = await db.execute(
                "SELECT AVG(latency_ms) FROM usage WHERE ts >= ? AND latency_ms > 0", (since,))
            avg = await cur.fetchone()
            cur = await db.execute(
                "SELECT COALESCE(purpose,''), COUNT(*), COALESCE(SUM(prompt_tokens),0), "
                "COALESCE(SUM(completion_tokens),0) FROM usage WHERE ts >= ? "
                "GROUP BY 1 ORDER BY 3 DESC", (since,))
            by_purpose = await cur.fetchall()
        pricing = config.load().get("pricing") or {}
        cost = 0.0
        models_out = []
        purpose_out = [(p or "chat", n, pt, ct) for p, n, pt, ct in by_purpose]
        hit_total = miss_total = 0
        for model, calls, pt, ct, hit, miss in by_model:
            pt = pt or 0
            ct = ct or 0
            price = pricing.get(model) or {}
            hit = int(hit or 0)
            miss = int(miss or 0)
            # 老数据没有缓存字段：整段按未命中算，别把成本算低
            miss_eff = miss if (hit or miss) else pt
            c = (miss_eff / 1e6 * float(price.get("in", 0))
                 + hit / 1e6 * float(price.get("cache_in", 0.5))
                 + ct / 1e6 * float(price.get("out", 0)))
            cost += c
            hit_total += hit
            miss_total += miss_eff
            models_out.append((model, calls, pt, ct, round(c, 4)))
        denom = hit_total + miss_total
        return {"calls": row[0], "prompt": row[1], "completion": row[2],
                "cost": round(cost, 4), "models": models_out,
                "latency": int((avg or [0])[0] or 0),
                "cache_hit": hit_total, "cache_miss": miss_total,
                "hit_rate": round(hit_total / denom, 3) if denom else 0.0,
                "purposes": purpose_out}

    def meme_allowed(self, m: dict) -> bool:
        """情绪强度到阈值才允许发表情。

        阈值按 valence 正负分档：正向 0.15 / 中性 0.20 / 负向 0.18。
        实测正向词 3 条就能到 0.28，负向词表缺口语词、同样条数只到 -0.20，
        单一阈值的结果就是「开心容易发表情、吐槽发不出来」。
        """
        try:
            v = float(m.get("valence", 0))
            e = float(m.get("energy", 0.7))
            # 强度 = 心情偏离 + 兴奋加成（精力高于静息值才加分）。
            # 原来用 |e-0.7| 取绝对值，把「累」也算成情绪强烈——疲倦时反而更容易发表情，
            # 这是语义上的错配：累是没力气，不是有情绪。
            intensity = abs(v) + max(0.0, e - 0.7) * 0.5
            tier = "positive" if v > 0 else ("negative" if v < 0 else "neutral")
            return intensity >= float(
                self.meme_thresholds.get(tier, self.meme_threshold))
        except Exception:
            return False

    async def _stream_raw(self, messages: list[dict], tools_arg=None, on_text=None,
                          max_tokens: int | None = None,
                          thinking: bool | None = None) -> dict:
        self._sync_llm_db()
        return await self.llm.stream(messages, tools_arg, on_text,
                                     max_tokens=max_tokens, thinking=thinking)

    def _style_mode(self) -> str:
        """rules = 旧规则；model = 只给人格和例子。未知值按 model。"""
        try:
            mode = str((config.load().get("chat") or {}).get("style_mode", "model")).lower()
        except Exception:
            mode = "model"
        return "rules" if mode == "rules" else "model"

    def _context_format(self) -> str:
        """compact = 单背景块；legacy = 旧的分块注入。未知值按 compact。"""
        try:
            mode = str((config.load().get("chat") or {}).get("context_format", "compact")).lower()
        except Exception:
            mode = "compact"
        return "legacy" if mode == "legacy" else "compact"

    def _thinking_for(self, user_text: str, images: list | None) -> bool | None:
        """这一轮要不要开思考。None = 用默认（开）。

        关思考的代价是「不再纠正错误前提、事实更松」，所以只在**明确闲聊**时关：
        短句、没有问号、没有技术词、不带图。其余一律交给思考模式。
        """
        if not self.thinking_casual:
            return None
        t = (user_text or "").strip()
        # 表情/贴图/短推论：这类轮次先接情绪，不需要先推理再回答。
        if prompt_mod.is_reaction_turn(
                t, len(images or []),
                bool(images) and prompt_mod.image_mode(t) == "fix"):
            return False
        if images:
            # 求助图仍开思考；分享/表情图不开。
            return None if prompt_mod.image_mode(t) == "fix" else False
        # 2026-11-10 阈值从 30 降到 20：实测「dsh 和 cc 都只是一个平台…」「还有什么和
        # harness 差不多价格好用的」这类 26–29 字的句子被当成闲聊关了思考，
        # 结果它既不搜也不推理，直接凭印象答。长句基本都不是纯闲聊。
        if len(t) >= 20:
            return None
        if t.endswith(("？", "?")) or "？" in t or "?" in t:
            return None
        if any(w in t for w in ("为什么", "怎么", "报错", "区别", "方案", "原理",
                                "帮我", "查", "多少", "能不能", "是不是该",
                                "价格", "多少钱", "推荐", "哪个好", "值不值",
                                "合适", "搜", "找")):
            return None
        return False

    async def _run_tools(self, msgs: list[dict], allow_meme: bool = True,
                         images: list[str] | None = None,
                         retry: bool = False,
                         on_text=None,
                         budget: int | None = None,
                         stats: dict | None = None,
                         thinking: bool | None = None,
                         allow_tools: bool = True, allow_web: bool = True,
                         show_tool_examples: bool = False) -> str:
        """工具循环本体在 tool_loop.py。这里只决定「哪些工具这轮不给用」。

        allow_tools=False 是给外人的：只放行 tools.PUBLIC_TOOLS（查资料、发表情），
        其余一律禁掉。工具层不知道谁在说话，note_list / kb_read 这类只读工具
        读的都是他的笔记和知识库，对别人同样不能开。
        """
        banned = set() if allow_meme else {"meme_send"}
        if not allow_tools:
            allowed = getattr(tools, "PUBLIC_TOOLS", frozenset())
            banned |= {((t.get("function") or {}).get("name") or "") for t in tools.TOOLS
                       if ((t.get("function") or {}).get("name") or "") not in allowed}
        if not allow_web:
            banned.update(("web_search", "web_open"))
        if retry:
            banned |= RETRY_BLOCKED
        messages = [*msgs[:-1], {"role": "system", "content":
            "回答前判断证据是否够。已给全信息就直接答。缺的是对话里提过的细节，先看引用、原话、图注或记忆。"
            "缺的是公开事实、时效信息或需要可靠来源，且允许联网时，带上实际对象和待核实的问题去搜。"
            "搜索摘要不足以支持结论时打开原文或继续查，交叉理解后再作答；分清事实、推断和仍不确定的部分。"
            "只有用户掌握的信息确实缺失时，才问一个具体问题。"
            "只写给用户看的回答。隐去内部知识目录、检索标签、工具语法和调用参数。"
            "背景资料与本轮无关时省略；需要时用自然语言解释与问题有关的部分。"}, *msgs[-1:]]
        return await tool_loop.run(
            self, messages,
            complete=lambda m, t: self.complete_raw(m, t, max_tokens=budget,
                                                    thinking=thinking),
            stream=lambda m, t, cb: self._stream_raw(m, t, cb, max_tokens=budget,
                                                     thinking=thinking),
            tools_mod=tools,
            tools_banned=banned, show_tool_examples=show_tool_examples,
            max_rounds=self.max_tool_rounds,
            tool_timeout=self.tool_timeout,
            total_timeout=self.tool_total,
            # 必须传原列表：meme_send 往它里面 append，用 `images or []` 会复制一份，
            # 图就永远到不了 turn.ai_images（2026-11-13 线上：调用 3 次都没发出去）。
            images=images if images is not None else [],
            on_text=on_text,
            stats=stats,
        )

    async def interpret_intent(self, session: str, text: str, **context):
        from app_core.turn_intent import interpret

        return await interpret(self, session, text, **context)

    async def reply(self, session: str, user_text: str,
                    images: list | None = None,
                    on_sentence=None,
                    store_text: str | None = None,
                    caps: Capability | None = None,
                    delivery_policy: DeliveryPolicy | None = None,
                    defer_history: bool = False,
                    reply_to: str | None = None,
                    quote_text: str | None = None, turn_intent=None,
                    image_sources: dict[str, list[str]] | None = None) -> tuple[list[str], list[str]]:
        """返回 (分段后的回复, 待发送的图片路径)。

        images：本轮用户发来的图片本地路径。只注入当前这轮，历史里只留文字标记。
        store_text：入库版本。文件正文这类超长输入只在**本轮**给模型看全文，
        历史里存一份压缩摘要，否则每轮都要把 8000 字重新发一遍。
        on_sentence：传了就启用流式，收到完整句子立即回调；返回值为还没发出去的尾巴。

        defer_history：为 True 时把 assistant 历史交给通道层，在每段发送确认后
        再写（runner 用）。直接调用方保持旧行为：commit 里写。

        流水线四段：prepare 取上下文 → compose 拼 prompt → generate 调模型 →
        commit 收尾。拆分不改行为，只是把原来一个 356 行的局部作用域切开。
        """
        # 用户消息要到生成结束后才落库。发布脚本若只看聊天库，会把这一轮当成已结束。
        inflight.mark(session)
        inflight.note_inbound(session)
        try:
            turn = await self._turn_prepare(
                session, user_text, images, store_text,
                quote_text=quote_text, reply_to=reply_to, turn_intent=turn_intent,
                image_sources=image_sources)
            control_reply = getattr(turn, "followup_control_reply", None)
            if control_reply:
                if not defer_history:
                    await self.record_delivery(session, control_reply)
                return [control_reply], []
            callback = on_sentence
            if on_sentence and not defer_history:
                async def legacy_callback(part: str):
                    # 旧宿主的回调只消费片段，通常不返回 bool。
                    return (await on_sentence(part)) is not False

                callback = legacy_callback
            await self._turn_compose(turn)
            await self._turn_generate(turn, callback)
            await self._turn_commit(turn, callback, defer_history=defer_history)

            def _plan(text: str) -> list[str]:
                policy = delivery_policy or DeliveryPolicy(max_chars=turn.seg_max)
                return plan_delivery(text, policy, caps)

            if on_sentence and turn.streamed:
                # 流式已经发出去的不再返回。失败段单独留着补发，不把全文再交回去。
                # 工具轮可能第一轮没成句、第二轮正文是非流式：这时 sent_parts 和
                # buffer 都是空的，正文在 turn.text 里，别把它吞了。
                tail = list(getattr(turn, "failed_parts", None) or [])
                if turn.buffer.strip():
                    tail.extend(_plan(turn.buffer))
                if not tail and not turn.sent_parts and (turn.text or "").strip():
                    tail = _plan(turn.text)
                return tail, turn.ai_images
            return _plan(turn.text), turn.ai_images
        finally:
            inflight.clear(session)

    def seg_max(self, m: dict) -> int:
        """情绪决定一条消息多长：累了碎一点，有精神可以连成句。

        分段长度和语气一样是「怎么说」的一部分。累的时候发一大段长句很违和；
        心情好、精力足时又切得太碎会像在蹦字。
        """
        try:
            e = float(m.get("energy", 0.7))
            v = float(m.get("valence", 0.0))
        except Exception:
            return 25
        if e < 0.3:
            return 18
        if e > 0.8 and v > 0.2:
            return 32
        return 25

    def _schedule_shadow(self, user_text: str, reply: str) -> None:
        """只记一条正例样本：他这句是笑/认同就记（之前只有纠错，正例全丢了）。

        「补一句」的影子（record_followup）已删除：从未产生样本、也没人消费。
        这里只写一行本地日志，无成本、不调模型。
        """
        try:
            shadow.record_positive(user_text, reply)
        except Exception as e:  # noqa: BLE001
            log.debug("正例记录失败：%s", e)

    def _schedule_summary(self, session: str) -> None:
        """把记忆总结丢到后台，不拖慢回复。同一会话不并发总结。"""
        if session in self._summarizing:
            return
        self._summarizing.add(session)

        async def _run() -> None:
            try:
                await self._maybe_summarize(session)
            except Exception as e:  # noqa: BLE001
                log.exception("记忆总结失败（%s）：%s", session, e)
            finally:
                self._summarizing.discard(session)

        asyncio.create_task(_run())
