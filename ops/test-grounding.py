"""对话事实来源和顺序核对，使用隔离数据库与合成输入。"""
from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import patch
from types import SimpleNamespace

from harness import bootstrap, check, report, temp_root

bootstrap()
from companion_core import EngineConfig, create_application


async def main() -> int:
    app = create_application(EngineConfig(temp_root("grounding-")))
    await app.start()
    core = app.core
    core.admin_uid = "owner"

    from app_core import correction, followup, grounding, memory_instant, prompt, summarize
    from app_core.chat_turn_base import Turn

    now = time.time()
    session = "private:owner"
    image = "[发了图片]（附了 1 张图）｜图注：视频作者选了24351，猜的是ISFJ。"
    options = "1.随心自在\n2.阖家幸福\n3.事业成就\n4.长久爱意\n5.精神世界"
    history = [
        {"role": "user", "content": image, "ts": now - 1600},
        {"role": "user", "content": "那我说一下我的排序你能猜出来我什么MBTI吗", "ts": now - 1500},
        {"role": "user", "content": "15243", "ts": now - 220},
        {"role": "assistant", "content": "5 被你压在最末，因为你说这个选项虚。", "ts": now - 200},
        {"role": "user", "content": options, "ts": now - 170},
        {"role": "user", "content": "那个图片是别人的视频，不是我的排序", "ts": now - 30},
    ]

    def read(messages):
        return next((json.loads(m["content"].split("\n", 1)[1]) for m in messages
                     if m["content"].startswith("[本轮引用与核对资料")), {})

    turn = Turn(session, "15243", [], None, True, "fixture", time.perf_counter(), history=history)
    await core._turn_compose(turn)
    facts = read(turn.msgs)
    check("回放进入正常模型入口，原话仍是末条", turn.msgs[-1] == {"role": "user", "content": "15243"})
    check("5 是第二，3 是最后，完整选项按名次排列",
          facts.get("已核对名次", {}).get("5") == 2 and facts.get("已核对名次", {}).get("3") == 5
          and facts.get("用户给的选项按排名") == ["随心自在", "精神世界", "阖家幸福", "长久爱意", "事业成就"])
    check("澄清归属保留原排序，截图的数字与助手猜测不进入核对结果",
          facts.get("用户输入的排序") == "15243" and "别人的视频" in facts.get("用户最新明确的纠正", "")
          and "24351" not in str(facts) and "选项虚" not in str(facts))
    check("追问沿用排序", read(grounding.evidence(history, "那像什么", now=now)).get("用户输入的排序") == "15243")
    check("单独数字、验证码与重复编号不猜排名",
          not read(grounding.evidence([], "15243", now=now))
          and not read(grounding.evidence(history, "验证码：15243", now=now))
          and "已核对名次" not in read(grounding.evidence(history, "15143", now=now)))
    check("没有选项表时只核对数字，不猜标签",
          "用户给的选项按排名" not in read(grounding.evidence(history[:2], "15243", now=now)))
    different = history + [{"role": "user", "content": "换个话题，新题的排序是什么", "ts": now}]
    check("新话题切断旧排序", not read(grounding.evidence(different, "那呢", now=now)))
    changed = history + [{"role": "user", "content": options.replace("精神世界", "成长空间"), "ts": now}]
    check("选项被另一张表替换后重新确认顺序",
          "用户输入的排序" not in read(grounding.evidence(changed, "那像什么", now=now)))
    quoted = read(grounding.evidence([], "你怎么看", quote="对方：我已经签完了", now=now))
    check("平台引用作为有标注的资料进入回复", quoted.get("平台引用内容（发言人未核实）") == "对方：我已经签完了")
    check("旧图注与发言区分，摘要区分转述",
          "机器人生成的图注" in prompt.dated_history(history)[0]["content"]
          and "[他转述]" in summarize.format_dialog([("user", "截图里说：已经签完了")]))
    dated = prompt.dated_history([{"role": "user", "content": "明天再看｜图注：明天交稿", "ts": now}])
    check("图内的相对日期沿用原文，用户自己的时间照常按消息锚定",
          "明天再看" not in dated[0]["content"] and "明天交稿" in dated[0]["content"])
    check("猜我的选择不自动搜索，明确要求查证时仍搜索",
          not core._wants_search(turn, "你猜猜我会怎么选") and core._wants_search(turn, "帮我查一下最新的价格"))
    with patch.object(followup, "control_turn") as control, patch.object(followup, "answer_turn") as answer:
        core._prepare_followup(turn, "截图里说：短租不用跟进了")
        core._prepare_followup(turn, "[发了图片]｜图注：短租已经签好了")
        core._prepare_followup(turn, "引用上一句：短租不用跟进了")
        check("引用或图注无法冒充用户执行跟进变更", not control.called and not answer.called)

    mixed = "你看一下这段\n对方说：我下周三要面试"
    check("合并后的转述不进入即时记忆，历史保留转述边界",
          not memory_instant.pick(mixed) and "[转述，发言人另有其人]" in grounding.history_text(mixed))
    multi = "对方说「这周有安排。\n我住在南京」\n我住在深圳"
    check("跨行引文不作为自述，后续独立原话仍可记忆", memory_instant.pick(multi) == "我住在深圳")
    with patch.object(followup, "control_turn", return_value=None) as control, patch.object(followup, "answer_turn", return_value={}):
        core._prepare_followup(turn, "你看一下这段\n对方说：短租不用跟进了")
        check("混合消息的跟进识别只能看用户自己说的片段", control.call_args.args[0] == "你看一下这段")

    writes = []

    async def remember(item):
        writes.append(item)

    async def adjudication(*args, **kwargs):
        return {"content": json.dumps({"is_correction": True, "kind": "fact", "claim": "他养了一条狗",
                                      "evidence": args[0][-1]["content"].splitlines()[-1].split("：", 1)[-1],
                                      "confidence": 0.99})}

    fake = SimpleNamespace(complete_raw=adjudication, _memory_port=lambda: SimpleNamespace(remember=remember))
    outcome = await correction.adjudicate(fake, "我没说过这句话", "你说你养了一只猫", write_pending=False)
    check("只否认旧话时，模型即使给高置信度也不能创设新事实", outcome == "pending" and not writes)
    await correction.adjudicate(fake, "我住在深圳", "你住在南京吧", write_pending=False, source_msg_id=7)
    check("纠正只保存经过核对的原话并关联发言记录",
          len(writes) == 1 and writes[0].text == "我住在深圳" and writes[0].payload["source_msg_ids"] == ["7"])

    docs = [{"doc_id": "dinner", "text": "昨天晚饭吃面"}, {"doc_id": "interview", "text": "面试地点是南山"}]
    with patch.object(core.memory, "recent", return_value=docs) as recent, patch.object(core.memory, "touch") as touch:
        selected = await core.memory.recall("之前的面试地点在哪里", qv=[])
        absent = await core.memory.recall("宽带套餐续费", qv=[])
        check("向量失败只挑有实词命中的记忆，找不到时留空",
              [m["doc_id"] for m in selected] == ["interview"] and selected[0]["recall_kind"] == "lexical"
              and absent == [] and recent.call_args.kwargs["touch"] is False and touch.call_args.args == (["interview"],))

    revoked = []

    class Port:
        async def supersede(self, ref, *args):
            revoked.append(ref)
            return True

    turn.injected_insights = ["a", "b"]
    turn.memories = [{"doc_id": "a", "text": "偏爱热闹"}, {"doc_id": "b", "text": "喜欢浓茶"}]
    turn.stored = "不对，我不是偏爱热闹"
    with patch.object(core, "_memory_port", return_value=Port()):
        await core._commit_memory(turn)
    check("纠正只推翻明确点名的洞察", revoked == ["a"], revoked)

    revoked.clear()
    turn.history = [{"role": "assistant", "content": "你说你住在南京"}]
    turn.stored = "我没说过这句话"
    turn.memories = [{"doc_id": "a", "text": "我住在南京"}, {"doc_id": "b", "text": "我住在苏州"}]
    with patch.object(core, "_memory_port", return_value=Port()):
        await core._commit_memory(turn)
    check("纯否认只撤回能对应的旧断言，否认本身不当成自述入库",
          revoked == ["a"] and not memory_instant.pick("我没说过我住在南京"), revoked)

    row = await core.remember(session, "user", "[发了图片]")
    writes = await asyncio.gather(core.append_to_message(row, "｜图注：第一份"),
                                  core.append_to_message(row, "｜图注：另一份"))
    saved = (await core.history(session, max_items=1))[0]["content"]
    check("两路并发图注只写一份", sum(writes) == 1 and saved.count("｜图注：") == 1)
    from app_core import learn_store, ocr, tool_loop
    from app_core.summary import SummaryMixin

    async def no_data(*args, **kwargs):
        return None

    async def save_over_window():
        await core.remember(session, "user", "[发了图片]（附了 1 张图）｜图注：右侧编号 B-527")
        for _ in range(6):
            await core.remember(session, "user", "换个话题，" + "背景信息" * 350)

    await save_over_window()
    with (patch.object(core, "_prepare_fetches", no_data),
          patch.object(core, "_prepare_vectors", no_data),
          patch.object(core, "_prepare_recall", no_data),
          patch.object(core, "_prepare_knowledge", no_data)):
        past = await core._turn_prepare(session, "还是回到刚才那张截图，右侧编号是什么？", [], None)
        await core._turn_compose(past)
    check("图注超出近期窗口后仍能按本会话回查并交给主模型",
          all("B-527" not in h["content"] for h in past.history)
          and any("历史图片证据" in m["content"] and "B-527" in m["content"] for m in past.msgs))
    check("其他会话拿不到本会话的图注", not await core.image_history("private:other"))
    from app_core import files

    files.SAVE_DIR.mkdir(parents=True, exist_ok=True)
    img = files.SAVE_DIR / "sample.png"
    img.write_bytes(b"fixture")
    msg_id = await core.remember(session, "user", "[发了图片]｜图注：原图引用测试")
    await core.save_image_references(session, msg_id, {"qq:42": [str(img)]})
    found = await core.recalled_image(session, "qq:42")
    check("持久图片引用命中同一会话和平台消息",
          found and found["id"] == msg_id and found["paths"] == [str(img)]
          and not await core.recalled_image(session, "feishu:42")
          and not await core.recalled_image("private:other", "qq:42"))

    async def fake_ocr(*args, **kwargs):
        return "RIGHT B-527"

    async def no_vision(*args, **kwargs):
        raise AssertionError("完整 OCR 无须再发一次图注请求")

    with patch.object(ocr, "ocr_text", fake_ocr), patch.object(core, "complete_raw", no_vision):
        caption = await core.caption_images(["fake.png"], text_first=True)
    check("OCR 文字进入共享图注，完整识别跳过视觉图注模型", "RIGHT B-527" in caption)

    long_query = "我在写作，" + "无关背景很多。" * 85 + "帮我查一下 DeepSeek-R1 的首次发布日期。"
    queries = []

    async def fetch(_turn, query):
        queries.append(query)
        return "来源与答案"

    with patch.object(core, "_fetch_search", fetch):
        await core._prepare_search(Turn(session, long_query, [], None, True, "lookup", time.perf_counter()), long_query)
    check("预搜索保留长问题末尾的检索目标", len(queries) == 1 and "DeepSeek-R1" in queries[0])
    check("可由已给信息解决的比较不预搜索", not core._wants_search(turn, "我给了两个数字，哪个更高？"))

    async def tool_gate(_core, messages, *, tools_banned, **kwargs):
        blocked = {"web_search", "web_open"} <= tools_banned
        routed = any("判断证据是否够" in msg["content"] for msg in messages)
        return "blocked" if blocked and routed else ""

    with patch.object(tool_loop, "run", tool_gate):
        result = await core._run_tools([{"role": "user", "content": "不用搜"}], allow_web=False, retry=True)
    check("禁搜约束覆盖主回复工具和重试", result == "blocked"
          and grounding.forbids_web("不要联网，按我发的图回答")
          and not grounding.forbids_web("他对我说：不用查这个。"))

    dialog = "前面是背景。" + "背景" * 230 + "。更正，日期改到周三。" + "背景" * 230 + "。最后地点定在东门。"
    rendered = summarize.format_dialog([("user", dialog, now)])
    check("总结保留长输入中间纠正和末尾决定", "日期改到周三" in rendered and "地点定在东门" in rendered)

    profile = {"职业与求职": {"text": "已入职", "changed": True, "evidence": "我已经入职了", "confidence": 0.9}}
    denied_profile = learn_store.apply_review({"profile": profile}, evidence_sources={6: "他说：我已经入职了。"})
    denied_partial = learn_store.apply_review({"profile": profile}, evidence_sources={6: "并不是我已经入职了。"})
    verified_profile = learn_store.apply_review({"profile": profile}, evidence_sources={7: "我已经入职了。"})
    check("自动复盘仅采信可回查的用户原话",
          not denied_profile["profile"] and not denied_partial["profile"] and verified_profile["profile"]
          and learn_store.profile_fields().get("职业与求职") == "已入职")
    from app_core import profile as profile_mod
    check("画像来源指向真实用户消息",
          profile_mod.load_source_ids(learn_store.PROFILE_JSON).get("职业与求职") == 7)

    pledge = "我会准备手写材料和面试文件。"
    unverified = ("下午提交简历", "我会下午提交简历。")
    candidates = [("准备手写材料", pledge), unverified]
    verified = SummaryMixin._verified_followups(candidates, [
        (128, "user", "朋友说：" + unverified[1], now), (129, "user", pledge, now)])
    check("总结里的跟进需有用户承诺，转述不会创建事项", len(verified) == 1 and verified[0][1] == 129)
    topic, message, ts, source_key = verified[0]
    inserted = followup.add(topic, source_ts=ts, source_msg_id=message, source_key=source_key)
    from app_core import followup_store
    existing_id = followup_store.id_for_text(topic)
    if existing_id:
        followup.mark_done([existing_id])
    duplicate = followup.add("准备面试文件", source_ts=ts, source_msg_id=message, source_key=source_key)
    check("同一条承诺换摘录重放保留已完成状态", inserted and not duplicate and bool(existing_id))

    from app_core import wiki
    pages = [
        {"title": "图像识别校验", "summary": "核对截图文字", "project": "图文工程", "status": "active",
         "source": "自撰", "verified_ts": 0, "mtime": now, "last_open_ts": 0, "stale_days": 0},
        {"title": "宽带套餐", "summary": "套餐价格", "project": "生活事项", "status": "active",
         "source": "自撰", "verified_ts": 0, "mtime": now, "last_open_ts": 0, "stale_days": 0},
    ]
    with patch.object(wiki, "pages", return_value=pages):
        found = wiki.index_text(query="图像识别校验怎么做")
        none = wiki.index_text(query="今天下雨怎么办")
        turn = Turn(session, "今天下雨怎么办", [], None, True, "knowledge", time.perf_counter())
        turn.query = "刚才提过图像识别校验 今天下雨怎么办"
        await core._prepare_knowledge(turn, turn.user_text)
    check("知识目录按当前话题筛选，上一轮的无关标题不进入本轮",
          "图像识别校验" in found and "宽带套餐" not in found and not none and not turn.knowledge)

    await app.stop()
    return report()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
