"""chat 的一轮流水线：prepare / compose / generate / commit。

从 chat.py 抽出来，目的是让 ChatCore 只剩组装和对外接口。
为了兼容测试对 chat.DB / chat.REPLY_MAX_TOKENS 的补丁，常量通过 _chat() 动态读取。
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import sys
import time
import uuid
from dataclasses import dataclass, field

from app_core import (attention, config, followup, intent, kb, learn, mood,
                      relational, shadow, wiki)
from app_core import profile as profile_mod
from app_core import prompt as prompt_mod
from app_core import self_check as self_check_mod
from app_core import web as web_mod
from app_core.prompt import split_reply

log = logging.getLogger("qqbot.chat")


def _chat():
    return sys.modules["app_core.chat"]


def _topic_overlap(a: str, b: str) -> bool:
    """两句有没有共同的具体词。

    待办只在「这轮真的聊到」时注入；泛泛的短句（「算了」「随便问问」）
    不该触发旧账。宁可漏提，也别主动翻。
    """
    import re

    _STOP = {"我们", "这个", "那个", "就是", "可以", "没有", "什么", "怎么",
             "还是", "但是", "因为", "所以", "如果", "已经", "现在", "时候"}

    def grams(s: str) -> set[str]:
        s = re.sub(r"\s+", "", s or "")
        out = {m.lower() for m in re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", s)}
        for run in re.findall(r"[\u4e00-\u9fa5]+", s):
            for i in range(len(run) - 1):
                g = run[i:i + 2]
                if g not in _STOP:
                    out.add(g)
        return out

    return bool(grams(a) & grams(b))


@dataclass
class Turn:
    """一轮回复的状态。

    拆 reply 的目的：原来 15 个阶段挤在同一个 356 行的局部作用域里，
    改任何一段都得读懂全部。现在阶段之间只传这一个对象，
    哪些是「本轮的」，一眼能看全。
    """
    session: str
    user_text: str
    images: list
    store_text: str | None
    owner: bool
    rid: str
    t0: float
    degraded: int = 0
    m: dict = field(default_factory=dict)
    stored: str = ""
    extra_system: list = field(default_factory=list)
    digest: str = ""
    history: list = field(default_factory=list)
    query: str = ""
    user_msg_id: int = 0
    mvec: list = field(default_factory=list)
    qv: list | None = None
    seg_max: int = 0
    memories: list = field(default_factory=list)
    knowledge: str = ""
    skills: list = field(default_factory=list)
    lessons: str = ""
    profile_block: str = ""
    open_items: str = ""
    msgs: list = field(default_factory=list)
    allow_meme: bool = True
    ai_images: list = field(default_factory=list)
    buffer: str = ""
    held: str = ""
    streamed: bool = False
    sent_parts: list = field(default_factory=list)
    text: str = ""
    img_budget: int = 0
    task_stats: dict = field(default_factory=dict)
    think: bool = False
    memory_trace: list = field(default_factory=list)
    self_check: str = ""
    meta_leak: bool = False


class ChatTurnMixin:
    async def _turn_prepare(self, session: str, user_text: str,
                            images: list | None, store_text: str | None) -> Turn:
        """取证：历史、记忆、知识、画像、情绪。"""
        turn = Turn(session=session, user_text=user_text, images=list(images or []),
                    store_text=store_text, owner=self._is_owner(session),
                    rid=uuid.uuid4().hex[:6], t0=time.perf_counter())
        # 只有本人那一轮才动记忆/情绪/知识/工具。别人可以聊，但聊的是一个
        # 「不记得他、也不知道我」的版本——否则他一句话就能把我的记忆念出来，
        # 或者把话写进我的库（2026-11-05 实测：陌生人问「我是谁」，它答了 QQ 号）。
        _ap = self._affect_port() if turn.owner else None
        turn.m = (_ap.read() if _ap is not None else mood.settle(mood.load())) if turn.owner else {
            "valence": 0.0, "valence_base": 0.0, "energy": 0.7, "social": 0.5}

        if store_text is not None:
            turn.stored = store_text
        elif turn.images:
            turn.stored = f"{user_text}（附了 {len(turn.images)} 张图）"
        else:
            turn.stored = user_text

        self._prepare_followup(turn, user_text)
        await self._prepare_search(turn, user_text)

        try:
            turn.digest = await self.get_digest(session)
        except Exception:
            turn.digest = ""
        # 还没有纪要时放宽历史预算：一上线就砍到 2500 会把老上下文丢了
        hist_budget = self.history_max_chars if turn.digest else max(self.history_max_chars, 4000)
        # 先读历史、后写本轮：否则刚写进去的这句会被 history() 读回来，
        # 再在 build_messages 末尾追加一次 —— 同一句话在 prompt 里出现两遍。
        turn.history = await self.history(session, max_chars=hist_budget)
        recent_user = [x["content"] for x in turn.history if x["role"] == "user"][-self.recall_query_items:]
        turn.query = " ".join(recent_user + [user_text])[-400:]

        turn.user_msg_id = await self.remember_safe(session, "user", turn.stored) or 0
        self.user_msg_id = turn.user_msg_id
        if not turn.user_msg_id:
            turn.degraded += 1

        await self._prepare_vectors(turn, user_text)
        await self._prepare_recall(turn, user_text)

        await self._prepare_knowledge(turn, user_text)

        await self._prepare_profile(turn)
        return turn

    # 分析类信号：他问的是「该不该/怎么办/形势」，不是随口聊。
    # 这类问题最容易空——先搜一轮，给它材料，而不是靠规则逼它。
    _ANALYTIC_HINTS = ("该不该", "怎么办", "为什么", "形势", "趋势", "分析",
                       "对比", "缺口", "问题在哪", "值不值", "怎么看")

    async def _prepare_search(self, turn: Turn, user_text: str) -> None:
        """分析类问题先搜一轮，把结果作为依据注入。

        为什么在代码里强制、而不是靠模型自己调工具：
        它觉得「这个我懂」，就不会搜，然后拿印象编数据（2026-11-11 实测）。
        """
        if not turn.owner:
            return
        try:
            if not bool((config.load().get("chat") or {}).get("force_search", True)):
                return
        except Exception:  # noqa: BLE001
            pass
        t = (user_text or "").strip()
        if len(t) < 4 or not intent.looks_analytical(t):
            return
        # 预搜索是锦上添花，不能吃掉整轮预算：web.search 自己的超时是 25s，
        # 而整轮 deadline 只有 45s —— 慢一次就只剩 20s 给模型，首字都可能出不来。
        try:
            res = await asyncio.wait_for(web_mod.search(t[:64], 3), timeout=5.0)
        except asyncio.TimeoutError:
            log.info("分析类预搜索超时（>5s），本轮不注入")
            return
        except Exception as e:  # noqa: BLE001
            log.debug("分析类预搜索失败：%s", e)
            return
        if not res or res.startswith("没搜到") or res.startswith("搜索失败"):
            return
        turn.extra_system.append(
            "【本轮搜索（可作依据；引用时标 [n]；没用到的别硬塞）】\n" + res[:1200])

    def _prepare_followup(self, turn: Turn, user_text: str) -> None:
        """他提到某件事的进展 → 对应 followup 推进状态，别反复追问同一件事。

        上一版只要命中完成信号词就关闭，「那个还没搞定」也会被当成办完了。
        """
        if turn.images or len(user_text) < 2:   # 「改完了」只有 3 个字，不能因此跳过
            return
        try:
            followup.confirm_pending(user_text)
            res = followup.resolve(user_text)
            if (res.get("done") or res.get("confirm")
                    or res.get("status") or res.get("drop")):
                followup.apply(res)
                log.info("[chat] followup %s", res)
            if res.get("done"):
                turn.extra_system.append(
                    "他刚说这件事办完了，你已经记成完成——"
                    "这轮别再问进展，也别再说「记得跟我说」。")
            if res.get("confirm"):
                names = followup.texts_for(res["confirm"])
                if names:
                    turn.extra_system.append(
                        "他像是在说「%s」已经办完了，但你没把握："
                        "在回复里顺口确认一句，别直接当成已完成。" % names[0])
            if res.get("drop"):
                turn.extra_system.append(
                    "他刚把之前提的那件事放掉了（忘了/算了/不弄了）——"
                    "这轮别提它，也别追问进展。")
        except Exception:
            pass

    async def _prepare_vectors(self, turn: Turn, user_text: str) -> None:
        """算本轮向量并观察情绪。

        两条向量一次算完：本轮这句（判话题推进）+ 召回 query（含前几轮）。
        之前分两次串行 embed，同一个 provider 要握手两遍，白等一趟 HTTP。
        """
        try:
            texts = [user_text] if user_text.strip() else []
            if turn.query and turn.query != user_text:
                texts.append(turn.query)
            if texts:
                vecs = await self.memory.embed(texts)
                if user_text.strip():
                    turn.mvec, turn.qv = vecs[0], vecs[-1]
                else:
                    turn.qv = vecs[0]
        except Exception:
            turn.mvec, turn.qv = [], None
        # 情绪要等向量出来才能算「推进 / 重复 / 换话题」，所以 observe 挪到这里。
        # 不是本人就不观察也不保存：情绪是全局单例，别人几句话能把他的心情带跑。
        if turn.owner:
            _ap = self._affect_port()
            if _ap is not None:
                _ap.observe(turn.m, user_text, vec=turn.mvec)
                _ap.write(turn.m)
            else:
                mood.observe(turn.m, user_text, vec=turn.mvec)
                mood.save(turn.m)
        turn.seg_max = self.seg_max(turn.m)

    async def _prepare_recall(self, turn: Turn, user_text: str) -> None:
        """召回记忆 + 二次召回影子。

        P1：fact 召回改走 `MemoryPort`（契约），结果与旧路径一致；
        `chat.memory_port=false` 可退回旧路径。
        """
        port = None
        try:
            port = self._memory_port() if turn.owner else None
            if port is not None:
                items = await port.recall(turn.query, ("fact",), 5, qv=turn.qv)
                turn.memories = [{
                    "doc_id": i.ref,
                    "text": i.text,
                    "meta": {"create_time": i.created,
                             "last_access_time": i.last_used},
                    "age_days": i.age_days,
                } for i in items]
            else:
                turn.memories = await self.memory.recall(turn.query, 5, qv=turn.qv) if turn.owner else []
        except Exception as e:  # noqa: BLE001
            log.debug("召回失败（%s），本轮无记忆", e)
            turn.memories = []
        # P4：这一轮读了什么，落一条轨迹，/trace 可查。
        if turn.owner and port is not None:
            turn.memory_trace = port.explain()
            try:
                import json as _json

                from app_core import paths as _paths

                with (_paths.DATA / "memory_trace.jsonl").open("a", encoding="utf-8") as f:
                    f.write(_json.dumps({
                        "ts": time.time(),
                        "session": turn.session,
                        "query": (turn.query or "")[:80],
                        "ids": [m.get("doc_id") for m in turn.memories],
                        "trace": turn.memory_trace,
                    }, ensure_ascii=False) + "\n")
            except Exception:  # noqa: BLE001
                pass
        # 二次召回（影子）：他这句是在回指上文时，用上一轮他的话再捞一次。
        # 只落盘、不改变本轮注入的 memories；touch=False 是为了不污染
        # 「被想起过」的记账——影子不该改变清理和重复降权的依据。
        try:
            if turn.owner and intent.refers_to_previous(user_text):
                prev_said = next(
                    ((h.get("content") or "").strip() for h in reversed(turn.history)
                     if h.get("role") == "user" and (h.get("content") or "").strip()),
                    "")
                if prev_said and prev_said != user_text:
                    extra = await self.memory.recall(prev_said, 3, touch=False)
                    shadow.record_second_recall(user_text, prev_said, extra)
        except Exception as e:  # noqa: BLE001
            log.debug("二次召回影子失败：%s", e)

    async def _prepare_knowledge(self, turn: Turn, user_text: str) -> None:
        """知识、技能、纠正清单——只有本人才注入。"""
        # 知识：优先注入索引（省 token、短查询更准）；索引还没建好时退回向量检索。
        # 不是本人就整段跳过——知识库里全是他自己的笔记和文档。
        if turn.owner:
            port = self._memory_port()
            if port is not None:
                try:
                    items = await port.recall(turn.query, ("knowledge",))
                    turn.knowledge = next(
                        (i.text for i in items if i.type == "knowledge"), "")
                except Exception as e:  # noqa: BLE001
                    log.debug("知识读取失败（%s）", e)
                    turn.knowledge = ""
            else:
                try:
                    turn.knowledge = wiki.index_text()
                except Exception:
                    turn.knowledge = ""
            if not turn.knowledge:
                try:
                    turn.knowledge = "\n\n".join(await kb.recall(turn.query, 3, qv=turn.qv))
                except Exception:
                    turn.knowledge = ""
            turn.skills = learn.skill_match(user_text, 2)
        # 他纠正过的事要真进 prompt，否则「学到了」只是写了个文件。
        # 按场景分流：「别瞎猜糊图」只在有图的那轮才有意义。
        # 纠正清单是全局文件，不是本人就别注入——里面写满了他的私事。
        if turn.owner:
            try:
                turn.lessons = "\n".join(
                    f"- {x}" for x in learn.recent_corrections(
                        6, scene="image" if turn.images else "text"))
            except Exception:
                turn.lessons = ""

    async def _prepare_profile(self, turn: Turn) -> None:
        """画像注入准备。

        画像一周才变一次，属于稳定前缀，放在纪要之前几乎不打断缓存。
        默认 shadow —— 只把「本来会注入的这 200 字」落盘，不改 messages。
        """
        try:
            _pfields = learn.profile_fields() if turn.owner else {}
            if _pfields:
                port = self._memory_port()
                if port is not None:
                    items = await port.recall(turn.query, ("profile",))
                    _ptext = next((i.text for i in items if i.type == "profile"), "")
                else:
                    _ptext = profile_mod.compact(_pfields, learn.profile_conf(),
                                                 learn.profile_seen())
                # YAML 里写 on/off 会被解析成布尔 True/False（YAML 1.1 的老毛病），
                # 这里统一归一成字符串，别让「写对了却没生效」再坑一次。
                _pm = (config.load().get("profile") or {}).get("inject", "shadow")
                _pmode = "on" if _pm is True else ("off" if _pm is False else str(_pm))
                if _pmode == "on":
                    turn.profile_block = _ptext
                if _pmode in ("on", "shadow"):
                    shadow.record_profile(_ptext, _pfields, _pmode)
        except Exception as e:  # noqa: BLE001
            log.debug("画像注入准备失败：%s", e)

    async def _turn_compose(self, turn: Turn) -> None:
        """拼 prompt：风格块、待办、相处状态、画像、知识合成一条 system 消息。"""
        # 这一轮怎么说：长度 / 形式 / 图片 / 时效 / 纠正 合成**一条**消息。
        # 合并的原因：原来五条 system 消息会互相打架（allow_short 就是打过的补丁），
        # 现在冲突规则集中在一个函数里，可测、只需查一处。
        if self._style_mode() != "model":
            try:
                recent_lens = [len(h.get("content") or "") for h in turn.history
                               if h.get("role") == "assistant"][-3:]
                _user_lens = [len(h.get("content") or "") for h in turn.history
                              if h.get("role") == "user"][-3:]
                block = prompt_mod.style_block(
                    turn.user_text, turn.history, turn.m, recent_lens,
                    burst=turn.user_text.count("\n") + 1,
                    images=len(turn.images) if turn.images else 0,
                    recent_user_lens=_user_lens)
                if block:
                    turn.extra_system = list(turn.extra_system) + [block]
            except Exception as e:  # noqa: BLE001
                log.debug("风格块生成失败：%s", e)

        # 他发表情/贴图/纯图时，优先用表情回应：文字接不住一张梗图。
        # 只在 reaction 轮加，正经问题不加，避免每轮都想发表情。
        try:
            _fix = bool(turn.images) and prompt_mod.image_mode(turn.user_text) == "fix"
            if prompt_mod.is_reaction_turn(turn.user_text, len(turn.images or []), _fix):
                turn.extra_system = list(turn.extra_system) + [
                    "（他发的是表情/贴图，不是题目：可以先用 meme_send 回一个，"
                    "再配一句很短的话；别分析画面。）"]
        except Exception as e:  # noqa: BLE001
            log.debug("表情回应提示失败：%s", e)

        # 当前轮就能"看见"图：图注原本只在事后补进历史，等于这一轮模型只看到
        # 「附了 1 张图」，问「这是什么」时答不出来。这里先给一次图注，再进 prompt。
        if turn.images:
            try:
                _cap = await self.caption_images(turn.images)
                if _cap:
                    turn.extra_system.append(
                        "[图注] " + _cap + "（这是图注，不是原图；细节不确定就别硬说）")
            except Exception as e:  # noqa: BLE001
                log.debug("当前轮图注失败：%s", e)

        # 此刻：他最近在聊什么、还挂着什么、情绪如何——状态，不是事实。
        try:
            from app_core import situation

            _sit = situation.describe(turn.session, turn.m)
            if _sit:
                turn.extra_system.append("[此刻]\n" + _sit)
        except Exception:  # noqa: BLE001
            pass

        # 关系小抄：称呼、梗、雷区——每轮注入，不走向量检索。
        try:
            from app_core import relation_notes

            _rel = relation_notes.load()
            if _rel:
                turn.extra_system.append("[我们之间]\n" + _rel)
        except Exception:  # noqa: BLE001
            pass

        # 我答应过还没跟进的事：只取 3 条、每条截短，塞进 prompt 让它记得。
        # 关键：**只注入本轮聊到相关词的**——否则它会在无关话题里翻旧账，
        # 把用户随口一提的事当成议程（2026-11-11 反馈：base_url 那句被反复追问）。
        try:
            _open = [x["text"][:40] for x in followup.pending(3)
                     if _topic_overlap(x.get("text") or "", user_text)]
            turn.open_items = "\n".join(f"- {x}" for x in _open)
        except Exception:  # noqa: BLE001
            turn.open_items = ""

        # 相处状态：说「他那边怎么样、这一轮该做什么」，不说「我心情如何」。
        try:
            _gap = await self.gap_hours(turn.session, turn.user_msg_id) if turn.owner else 0.0
            # 这里要的是**他**说要做还没做的事，不是「我答应过他、还没跟进的事」。
            # 两者都存在 followup 里，但方向相反：一个是他自己欠着，一个是我欠他。
            _his = followup.due_for_asking(3) if turn.owner else []
            _mood_text = relational.describe(
                turn.m, _gap, len(_his), _his[0]["text"] if _his else "")
        except Exception:  # noqa: BLE001
            _mood_text = mood.describe(turn.m)
        turn.msgs = self.build_messages(turn.history, turn.user_text, turn.memories,
                                        _mood_text, turn.knowledge, turn.skills,
                                        turn.images, turn.extra_system, turn.digest,
                                        lessons=turn.lessons, open_items=turn.open_items,
                                        profile=turn.profile_block,
                                        persona=self.persona_for(turn.session),
                                        style_mode=self._style_mode(),
                                        context_format=self._context_format())
        await self._log_prompt(turn.session, turn.msgs, digest=turn.digest,
                               images=turn.images)
        # 表情包工具常驻：以前按情绪强度条件注入，工具表时有时无，模型学不出
        # 稳定预期，结果一次都不发。频率改由「每轮最多一张 + 24h 去重」控制。
        turn.allow_meme = True

    async def _turn_generate(self, turn: Turn, on_sentence) -> None:
        """调模型（含流式、重试、降级），产出 turn.text / buffer / sent_parts。"""
        _model_first = self._style_mode() == "model"

        async def sink(delta: str) -> None:
            """流式增量 → 攒够一句就交给 on_sentence 发出去。

            疑似的「结尾建议」先扣住：等下一句到了说明它不是结尾，再补发；
            一直没下一句，就说明它真是结尾 —— 丢掉（这条实测模型改不掉，只能事后剪）。
            """
            turn.streamed = True
            turn.buffer += delta
            # 自检标签在正文前一行：等它写完（换行）再剥，别把半截标签当正文发。
            if not getattr(turn, "_sc_done", False):
                _tag, _body = self_check_mod.parse(turn.buffer)
                if _tag:
                    # 标签完整（读到「来源=」）就立刻剥，不等换行——
                    # 模型经常用空格而不是换行分隔，等换行会把标签当正文发出去。
                    turn.self_check = _tag
                    turn.buffer = _body
                    turn._sc_done = True
                elif (self_check_mod.looks_like_tag(turn.buffer)
                        and len(turn.buffer) < 200):
                    return
                else:
                    turn._sc_done = True
            cut = max(turn.buffer.rfind(c) for c in "。！？!?~…\n")
            if cut >= 0 and cut < len(turn.buffer) - 1:
                chunk, turn.buffer = turn.buffer[:cut + 1], turn.buffer[cut + 1:]
                for part in split_reply(chunk, turn.seg_max):
                    # 兜底：不管前面剥没剥，发送前再剥一次（幂等，正文里不会有 [自检]）
                    _p_tag, part = self_check_mod.parse(part)
                    if _p_tag and not turn.self_check:
                        turn.self_check = _p_tag
                    if not _model_first:
                        part = prompt_mod.soften_tone(part)  # 去掉「哈哈哈」这类轻浮语气
                    if not part.strip():
                        continue
                    if self_check_mod.is_meta(part):
                        log.warning("[chat] 丢弃元叙述片段：%s", part[:60])
                        turn.meta_leak = True
                        continue
                    if turn.held:
                        await on_sentence(turn.held)
                        turn.sent_parts.append(turn.held)
                        turn.held = ""
                    # 规则模式才扣结尾建议；model 模式让模型自己决定
                    if not _model_first and not prompt_mod.wants_advice(turn.user_text) \
                            and prompt_mod.is_advice_sentence(part):
                        turn.held = part
                    else:
                        await on_sentence(part)
                        turn.sent_parts.append(part)

        turn.img_budget = max(self.max_tokens, _chat().REPLY_MAX_TOKENS)
        turn.think = self._thinking_for(turn.user_text, turn.images)
        try:
            turn.text = await self._run_tools(
                turn.msgs, turn.allow_meme, turn.ai_images,
                on_text=sink if on_sentence else None,
                budget=turn.img_budget, stats=turn.task_stats,
                thinking=turn.think, allow_tools=turn.owner)
            if not turn.text:
                # 重试也要守住白名单：漏传 allow_tools 会默认 True，
                # 等于给外人一次「首次回复为空」的机会拿回全部工具（能读笔记和记忆）。
                turn.text = await self._run_tools(
                    turn.msgs, turn.allow_meme, turn.ai_images, retry=True,
                    budget=turn.img_budget, stats=turn.task_stats,
                    thinking=turn.think, allow_tools=turn.owner)
        except Exception as e:  # noqa: BLE001
            log.warning("生成回复失败：%s", e)
            turn.degraded += 1
            turn.text = ""
            # 流式在首字之前就断了：这时什么都没发出去，退回非流式重来一次，
            # 别直接把兜底的「在」甩给用户。已经吐过字就不能重来。
            if on_sentence and not turn.streamed:
                try:
                    turn.text = await self._run_tools(
                        turn.msgs, turn.allow_meme, turn.ai_images,
                        budget=turn.img_budget, stats=turn.task_stats,
                        thinking=turn.think, allow_tools=turn.owner)
                    log.info("流式失败，回退非流式成功")
                except Exception as e2:  # noqa: BLE001
                    log.warning("回退非流式也失败：%s", e2)
        # 元叙述：模型把「怎么组织回复」写进了正文。流式片段已在 sink 里拦掉，
        # 这里再兜一次整段（非流式路径）；还没发出去就重试一次，且关掉思考。
        if self_check_mod.is_meta(turn.text or turn.buffer):
            log.warning("[chat] 整段是元叙述，丢弃后重试：%s",
                        (turn.text or turn.buffer)[:80])
            turn.meta_leak = True
            turn.text, turn.buffer = "", ""
            if not turn.sent_parts:
                try:
                    turn.text = await self._run_tools(
                        turn.msgs, turn.allow_meme, turn.ai_images, retry=True,
                        budget=turn.img_budget, stats=turn.task_stats,
                        thinking=False, allow_tools=turn.owner)
                    turn.streamed = False      # 重试是非流式，交回给 reply 发
                except Exception as e:  # noqa: BLE001
                    log.warning("元叙述重试失败：%s", e)
        if not turn.text and not turn.sent_parts:
            turn.text = random.choice(_chat().FALLBACK_LINES)
        # 这一轮到底费不费劲：查了几轮工具、答了多长。问一句答一句几乎不耗，
        # 查三次资料再写长答案才该累——按条数扣钱会把这两件事算成一样。
        try:
            if turn.owner:
                mood.apply_task(turn.task_stats.get("rounds", 0),
                                len(turn.text or turn.buffer))
        except Exception as e:  # noqa: BLE001
            log.debug("结算任务精力失败：%s", e)
        log.info("[chat] req %s %dms streamed=%s degraded=%d rounds=%d tools=%d "
                 "out=%d chars: %s",
                 turn.rid, int((time.perf_counter() - turn.t0) * 1000), turn.streamed,
                 turn.degraded, int(turn.task_stats.get("rounds", 0)),
                 int(turn.task_stats.get("tools", 0)),
                 len(turn.text or turn.buffer),
                 (turn.text or turn.buffer)[:60].replace(chr(10), " "))

    async def _turn_commit(self, turn: Turn, on_sentence) -> None:
        """收尾：影子、注意力记账、剪建议、写历史、排下一次总结。"""
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

        # 注意力记账：这条回复有多少在说他、多少在说自己。
        # 不评判好坏，只看两周后的趋势——自指率往上走，就是开始表演了。
        if turn.owner:
            try:
                _ar = attention.self_ref_ratio(turn.text or turn.buffer)
                shadow.record_attention(turn.text or turn.buffer, _ar["ratio"],
                                        _ar["self"], _ar["you"], turn.user_text)
            except Exception as e:  # noqa: BLE001
                log.debug("注意力记账失败：%s", e)

        # 事后硬剪只属于 rules 模式；model 模式让模型自己决定，不再二次加工。
        # **但他在征求意见时不剪**——「该不该/怎么办」的答案本身就是建议。
        _model_first = self._style_mode() == "model"
        _allow_advice = True if _model_first else prompt_mod.wants_advice(turn.user_text)
        if turn.streamed:
            if turn.held and not _allow_advice:
                # 扣住的这句一直没等到下一句，说明它就是结尾 —— 丢掉，除非后面还有尾巴
                if not turn.buffer.strip():
                    log.info("[chat] 剪掉结尾建议（流式）：%s", turn.held[-40:])
                else:
                    try:
                        await on_sentence(turn.held)
                        turn.sent_parts.append(turn.held)
                    except Exception as e:  # noqa: BLE001
                        log.debug("流式补发失败：%s", e)
                turn.held = ""
            elif turn.held:
                try:
                    await on_sentence(turn.held)
                    turn.sent_parts.append(turn.held)
                except Exception as e:  # noqa: BLE001
                    log.debug("流式补发失败：%s", e)
                turn.held = ""
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
            sent_text = "".join(turn.sent_parts) + (turn.buffer if turn.buffer.strip() else "")
        else:
            sent_text = turn.text or turn.buffer
        # 入库前再剥一次：流式路径的 sent_parts 可能还带着标签（发送侧已兜底，
        # 这里保证历史里不会出现 [自检]）。
        _h_tag, sent_text = self_check_mod.parse(sent_text)
        if _h_tag and not turn.self_check:
            turn.self_check = _h_tag
        if not await self.remember_safe(turn.session, "assistant", sent_text):
            turn.degraded += 1
        if self.admin_uid and turn.session == f"private:{self.admin_uid}":
            self._schedule_summary(turn.session)
