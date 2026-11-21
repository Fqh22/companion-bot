"""[orchestration] 一轮对话的收尾：落库 / 记忆 / 观测 / 发送收尾（POST_WRITE 阶段）。

从 chat_turn.py 拆出来的原因：这四段只依赖「库 + 记忆端口 + 观测组件」，
和准备/组装/生成三阶段无关；原来 _turn_commit 一个函数 235 行。
Mixin 组合：ChatTurnMixin(CommitMixin)。
"""
from __future__ import annotations

import asyncio
import json
import time

from app_core import correction, memory_instant, shadow
from app_core import prompt as prompt_mod
from app_core import self_check as self_check_mod
from app_core.chat_turn_base import POST_WRITE, log


class CommitMixin:

    async def _turn_commit(self, turn: Turn, on_sentence,
                           defer_history: bool = False) -> None:
        """收尾：落库 → 记忆 → 观测 → 发送收尾。四段顺序不能换。"""
        turn.stage = POST_WRITE
        await self._commit_write_user(turn)
        await self._commit_memory(turn)
        self._commit_observe(turn)
        await self._commit_finalize(turn, on_sentence,
                                    defer_history=defer_history)

    async def _commit_write_user(self, turn: Turn) -> None:
        """1/4 落库：本轮 user 消息（shield 住取消）、统计行、图注写回。"""
        # 本轮 user 消息在这里才落库：prepare 只取证，写库统一在 POST_WRITE。
        # shield：45 秒硬线取消时也要把这条消息写完，否则历史和图注都会缺。
        try:
            turn.user_msg_id = await asyncio.shield(
                self.remember_safe(turn.session, "user", turn.stored)) or 0
        except Exception as e:  # noqa: BLE001
            log.debug("写本轮用户消息失败：%s", e)
            turn.user_msg_id = 0
        if not turn.user_msg_id:
            turn.degraded += 1
        # 图注写回：本轮唯一的「渲染时生成、写库时落盘」的块。
        # shield 是给取消/超时路径用的——45 秒硬线会补发已生成部分并结束本轮，
        # 若写回被 cancel，用户翻聊天记录会缺图注，而 36/36 依旧全绿。
        # 每轮一个统计行：prepare_ms（取证占用）、first_ms（首字）、total_ms、是否超预算。
        # 没有这个，S2 只能看到「整轮从 15.4 降到 12」，看不到首字。
        try:
            from app_core import paths as _p2

            _first = (int((turn.first_token_at - turn.t0) * 1000)
                      if turn.first_token_at else -1)
            with (_p2.DATA / "turn_stats.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "ts": time.time(), "ses": (turn.session or "")[:24], "rid": turn.rid,
                    "prepare_ms": int(turn.prepare_ms), "first_ms": _first,
                    "total_ms": int((time.perf_counter() - turn.t0) * 1000),
                    "over_budget": bool(turn.prepare_over_budget),
                    "degraded": int(turn.degraded), "streamed": bool(turn.streamed),
                    "parallel": bool(turn.parallel), "budget_exhausted": bool(turn.budget_exhausted),
                    "fetches": turn.fetch_metrics,
                    "meta_leak_partial": bool(turn.meta_leak_partial),
                }, ensure_ascii=False) + "\n")
        except Exception as e:  # noqa: BLE001
            log.debug("轮次统计写入失败：%s", e)

        if turn.caption_text and turn.user_msg_id:
            try:
                _suffix = "｜图注：" + turn.caption_text
                await asyncio.shield(self.append_to_message(turn.user_msg_id, _suffix))
                turn.stored = (turn.stored or "") + _suffix
            except Exception as e:  # noqa: BLE001
                log.debug("图注写回失败：%s", e)
                from app_core import alerts as _alerts
                _alerts.record("warn", "caption_writeback_failed", str(e)[:200], turn.session)

    async def _commit_memory(self, turn: Turn) -> None:
        """2/4 记忆：用户否定推翻洞察、即时记忆落库。"""
        # 用户否定：把本轮注入的洞察标成被推翻。
        # 这是 insight 唯一的纠错入口——没有它，一个错的理解会一直影响后续对话，
        # 而且用户否定了也没人知道（和欠费那晚同类：错了但不响）。
        _cport = self._memory_port() if turn.injected_insights else None
        if _cport and correction.looks_like_correction(turn.stored or ""):
            _n = 0
            for _did in turn.injected_insights:
                try:
                    if await _cport.supersede(_did, "user_correction", "用户否定"):
                        _n += 1
                except Exception as e:  # noqa: BLE001
                    log.debug("推翻洞察失败：%s", e)
            if _n:
                log.info("用户否定，推翻 %d 条洞察", _n)
                from app_core import alerts as _alerts2
                _alerts2.record("info", "insight_corrected", "用户否定 %d 条" % _n, turn.session)
        # 即时记忆：明确自述立刻落库，不等批量总结。
        # 现在要等「8 轮 / 40 条 / 闲置 30 分钟」——你上午说「下周三面试」，
        # 之后不再说话，这句要等 30 分钟后的下一条消息才进记忆。
        # 只写最窄的一类（memory_instant 里那几条模式），importance 0.35 可被 cleanup 清。
        if turn.owner and turn.stored and turn.user_msg_id:
            try:
                _inst = memory_instant.pick(turn.stored)
                _port = self._memory_port() if _inst else None
                if _port:
                    from app_core.memory_port import MemoryItem

                    _vec = await self.memory.embed([_inst])
                    await _port.remember(MemoryItem(
                        type="fact", text=_inst, source="instant", confidence=0.35,
                        payload={"session": turn.session, "topics": ["即时"],
                                 "key_facts": [_inst],
                                 "vec": (_vec[0] if _vec else None),
                                 "range_key": "instant:%s:%s" % (turn.session, turn.user_msg_id),
                                 # 即时记忆能精确到「他这一句话」，把原文指针存下来
                                 "source_msg_ids": [str(turn.user_msg_id)]}))
            except Exception as e:  # noqa: BLE001
                log.debug("即时记忆失败：%s", e)
                from app_core import alerts as _alerts
                _alerts.record("warn", "instant_memory_failed", str(e)[:200], turn.session)

    def _commit_observe(self, turn: Turn) -> None:
        """3/4 观测：自检标签、影子样本、无来源断言、注意力记账。只记录不改行为。"""
        # 自检标签：剥掉、记一条。P1 只观测，不改回复。
        try:
            _tag, _body = self_check_mod.parse(turn.text or "")
            if _tag:
                turn.self_check = _tag
                turn.text = _body
            if turn.self_check:
                from app_core import paths as _paths

                with (_paths.DATA / "self_check.jsonl").open("a", encoding="utf-8") as f:
                    f.write(json.dumps({
                        "ts": time.time(), "session": turn.session,
                        "tag": turn.self_check,
                        "query": (turn.user_text or "")[:60],
                        "reply": (turn.text or "")[:120],
                    }, ensure_ascii=False) + "\n")
        except Exception as e:  # noqa: BLE001
            log.debug("自检标签记录失败：%s", e)
        # 影子记录：不改行为，只攒样本（补一句的候选 + 正反馈样本）。
        # 只记本人的：这些样本是用来调他的机器人语气/频率的基线，
        # 混进陌生人的对话（还是鲸鱼人格）会把基线带偏。
        if turn.owner:
            try:
                self._schedule_shadow(turn.user_text, turn.text or turn.buffer)
            except Exception as e:  # noqa: BLE001
                log.debug("影子记录失败：%s", e)

            # 无来源断言检测（只记录，不改回复）：先摸误报率，再决定要不要拦截。
            try:
                import json as _json

                from app_core import fact_check
                from app_core import paths as _paths

                _allowed = "\n".join(
                    [str(m.get("text") or "") for m in turn.memories]
                    + [turn.knowledge or "", turn.profile_block or "",
                       turn.user_text or ""])
                _bad = fact_check.unsourced(turn.text or turn.buffer, _allowed)
                if _bad:
                    with (_paths.DATA / "fact_check.jsonl").open("a", encoding="utf-8") as f:
                        f.write(_json.dumps({
                            "ts": time.time(), "session": turn.session,
                            "claims": _bad, "text": (turn.text or "")[:200],
                        }, ensure_ascii=False) + "\n")
            except Exception as e:  # noqa: BLE001
                log.debug("事实检查记录失败：%s", e)

    async def _commit_finalize(self, turn: Turn, on_sentence,
                               defer_history: bool = False) -> None:
        """4/4 收尾：剪建议 → 写历史 → 现实校验 → 后台纠正 → 排下一次总结。

        `defer_history=True`：由通道层（runner）在**发送确认后**写历史。
        引擎这里只负责算"这一轮说了什么"，不再先入库后发送。
        """
        # 事后硬剪只属于 rules 模式；model 模式让模型自己决定，不再二次加工。
        # **但他在征求意见时不剪**——「该不该/怎么办」的答案本身就是建议。
        _model_first = self._style_mode() == "model"
        _allow_advice = True if _model_first else prompt_mod.wants_advice(turn.user_text)
        if turn.streamed:
            # 扣住的这句一直没等到下一句：允许建议时发，不允许且后面没尾巴就丢。
            _tail_ready = True
            if turn.held:
                _send_held = _allow_advice or bool(turn.buffer.strip())
                if not _send_held:
                    log.info("[chat] 剪掉结尾建议（流式）：%s", turn.held[-40:])
                else:
                    try:
                        if await on_sentence(turn.held):
                            turn.sent_parts.append(turn.held)
                        else:
                            # 没发出去：放回待发尾段，交给通道层重试；
                            # 绝不能只因为"试过了"就写进历史。
                            turn.buffer = turn.held + (turn.buffer or "")
                            _tail_ready = False
                    except Exception as e:  # noqa: BLE001
                        log.debug("流式补发失败：%s", e)
                        turn.buffer = turn.held + (turn.buffer or "")
                        _tail_ready = False
                turn.held = ""
            # 尾段在收尾阶段发：仍在整轮 watchdog 的时限内，且返回值决定
            # 它是否算"已发出"（历史只写确认过的部分）。
            if _tail_ready and turn.buffer.strip():
                _tail = turn.buffer
                try:
                    if await on_sentence(_tail):
                        turn.sent_parts.append(_tail)
                        turn.buffer = ""
                except Exception as e:  # noqa: BLE001
                    log.debug("流式尾段发送失败：%s", e)
        elif not _model_first:
            try:
                # 先去掉轻浮语气词，再剪结尾建议；顺序反了会把「哈哈，建议你…」剪歪
                _soft = prompt_mod.soften_tone(turn.text or turn.buffer)
                if _soft and _soft != (turn.text or turn.buffer):
                    turn.text = _soft
                    turn.buffer = _soft
                _cut, _hit = prompt_mod.strip_advice_tail(turn.text or turn.buffer,
                                                          allow_advice=_allow_advice)
                if _hit:
                    log.info("[chat] 剪掉结尾建议：%s", (turn.text or turn.buffer)[-40:])
                    turn.text = _cut
                    if _cut != turn.buffer:
                        turn.buffer = _cut
            except Exception as e:  # noqa: BLE001
                log.debug("结尾建议修剪失败：%s", e)

        # 写历史的是「用户真正看到的那几句」：流式用已发出的片段 + 还要补发的尾巴，
        # 非流式用剪完建议、去掉语气词之后的文本。放在剪之后再写，否则历史里留着
        # 用户根本没看到的话，下一轮它会以为「我上面不是说过了」。
        if turn.streamed:
            # 通道层（defer_history）只算确认发出去的片段；没发出去的尾段交给
            # runner 重试，不进历史。直接调用方（defer_history=False）保持旧语义：
            # 回调可能不返回 bool，尾段仍按"将要发出"计入。
            sent_text = "".join(turn.sent_parts)
            if turn.buffer.strip() and not defer_history:
                sent_text += turn.buffer
        else:
            sent_text = turn.text or turn.buffer
        # 工具轮：第一轮流式内容没成句、第二轮正文又是非流式时，上面会是空串。
        # 历史里留空，下一轮它就不知道自己说过什么（2026-11-13 线上第 2998 条）。
        if not sent_text.strip() and (turn.text or "").strip():
            sent_text = turn.text
        # 入库前再剥一次：流式路径的 sent_parts 可能还带着标签（发送侧已兜底，
        # 这里保证历史里不会出现 [自检]）。
        _h_tag, sent_text = self_check_mod.parse(sent_text)
        if _h_tag and not turn.self_check:
            turn.self_check = _h_tag
        # 现实校验：只记录，不改回复。影子跑一周，看误报率再决定要不要重写。
        try:
            from datetime import datetime as _dt

            from app_core import reality as _reality

            _reality.check(sent_text, _dt.now().hour,
                           "无" if "来源=无" in (turn.self_check or "") else "",
                           turn.rid, turn.session)
        except Exception:  # noqa: BLE001
            pass
        if not sent_text.strip():
            # 2026-11-13 第 3062 条：日志有 out=20 字、也发送了 1 段，历史却是空。
            # 这里把当时的流式状态打出来，下次复现就能定位是哪条分支把文本丢了。
            log.warning("[chat] 回复文本为空，跳过写历史：streamed=%s parts=%d "
                        "buffer=%d text=%d meta=%s", turn.streamed,
                        len(turn.sent_parts), len(turn.buffer or ""),
                        len(turn.text or ""), turn.meta_leak)
        elif defer_history:
            # 通道层拿到每段的发送结果后写；这里不抢先入库。
            pass
        elif not await self.remember_safe(turn.session, "assistant", sent_text):
            turn.degraded += 1
        # 当场纠正：命中信号 + 上一轮有回复，后台判断（不阻塞本轮）。
        # 判断「是不是纠正、纠正什么、事实还是行为」需要一次小模型调用，
        # 放同步会吃掉 45 秒预算——照图注模式异步跑。
        try:
            _prev = next((m.get("content") or "" for m in reversed(turn.history)
                          if m.get("role") == "assistant"), "")
            # 只认本人的纠正：写入的是全局 corrections 和本人的 fact 记忆，
            # 让外人触发等于让别人替你改记忆（见 510 行「里面写满了他的私事」）。
            if turn.owner and _prev and correction.looks_like_correction(turn.user_text):
                try:
                    from app_core import config as _cfg_mod
                    if str((_cfg_mod.load().get("persona") or {}).get("self_learn", "off")) == "shadow":
                        shadow.record_persona_drift("correction", turn.user_text, turn.rid)
                except Exception:  # noqa: BLE001
                    pass
                asyncio.create_task(correction.adjudicate(
                    self, turn.user_text, _prev, turn.rid, turn.session))
        except Exception:  # noqa: BLE001
            pass
        # 用统一的主人会话解析：只配飞书主人时也要触发总结，不能只看 QQ 号。
        _owner_session = self.private_session()
        if _owner_session and turn.session == _owner_session:
            self._schedule_summary(turn.session)
