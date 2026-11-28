"""离线回归组，跨组状态由入口显式传递。"""
from offline_regression.fixtures import (
    asyncio, json, sqlite3, time, datetime, TMP, chat_mod, config_mod, files_mod,
    followup_mod, kb_mod, learn_mod, followup_store_mod, remind_mod, REAL_KB_RECALL, check,
)


async def run(core, fake_embed, learn_store_mod, mem, relational_mod):
    # ---------- E. 记忆总结：水位推进、不重复 ----------
    con = sqlite3.connect(TMP / "chat.db")
    with con:
        con.execute("DELETE FROM summaries")
        con.execute("DELETE FROM messages")
    con.close()

    session = "private:100000000"  # 测试用假 QQ 号，不涉及真实账号
    for i in range(10):
        await core.remember(session, "user", f"问题{i}")
        await core.remember(session, "assistant", f"回答{i}")

    summary_inputs = []

    async def fake_complete_summary(msgs, max_tokens=None, temperature=None,
                                    json_mode=False):
        # 滚动纪要和矛盾检测走的是同一个函数，别把它们算进「总结次数」
        head = msgs[0].get("content") or ""
        if "滚动纪要" in head:
            return "这段时间在调记忆水位，顺手把滚动纪要也接上了。"
        if "核对记忆有没有自相矛盾" in head:
            return '{"pairs": []}'
        summary_inputs.append(msgs[-1]["content"])
        return json.dumps({"summary": f"第{len(summary_inputs)}段记忆",
                           "key_facts": ["f"], "topics": ["t"], "importance": 0.6},
                          ensure_ascii=False)

    core.complete_summary = fake_complete_summary
    mem.embed = fake_embed

    # 矛盾检测走的是 complete_summary(..., temperature=0.0)。桩的签名以前落后，
    # 调用直接 TypeError 被上层的 except 吞掉——648 项全绿，但这条路径从没跑过。
    # 这里显式走一遍，签名再漂移就会红。
    contra_calls = []

    async def fake_contra(msgs, max_tokens=None, temperature=None):
        contra_calls.append(temperature)
        return "[]"

    _old_similar = mem.similar_active
    mem.similar_active = lambda *a, **k: [{"doc_id": "old-doc", "score": 0.9,
                                           "text": "旧记忆"}]
    core.complete_summary = fake_contra
    try:
        await core._check_contradictions("new-doc", {"summary": "新事实", "key_facts": []},
                                         [0.1, 0.2])
    finally:
        mem.similar_active = _old_similar
        core.complete_summary = fake_complete_summary
    check("矛盾检测真的走到模型（temperature=0.0）", contra_calls == [0.0], str(contra_calls))

    await core._maybe_summarize(session)
    await core._maybe_summarize(session)  # 没有新消息
    check("无新消息时不会重复总结", len(summary_inputs) == 1, f"calls={len(summary_inputs)}")

    con = sqlite3.connect(TMP / "chat.db")
    last_id = con.execute("SELECT last_id FROM summaries WHERE session=?", (session,)).fetchone()[0]
    max_id = con.execute("SELECT MAX(id) FROM messages WHERE session=?", (session,)).fetchone()[0]
    docs = con.execute("SELECT COUNT(*) FROM summaries").fetchone()[0]
    con.close()
    check("水位推进到最新消息", int(last_id) == int(max_id), f"{last_id} vs {max_id}")
    check("summaries 只有一行", docs == 1)

    for i in range(10, 18):
        await core.remember(session, "user", f"问题{i}")
        await core.remember(session, "assistant", f"回答{i}")
    await core._maybe_summarize(session)
    check("新对话触发第二次总结", len(summary_inputs) == 2, f"calls={len(summary_inputs)}")
    check("第二次总结不含旧内容", "问题0" not in summary_inputs[-1], summary_inputs[-1][:60])
    check("第二次总结含新内容", "问题17" in summary_inputs[-1] or "问题10" in summary_inputs[-1])

    # ---------- E2. 滚动纪要 ----------
    dg = await core.get_digest(session)
    check("总结后生成滚动纪要", bool(dg) and "纪要" in dg, (dg or "")[:30])
    check("历史预算默认 6000", core.history_max_chars == 6000, str(core.history_max_chars))
    m_with = core.build_messages([{"role": "user", "content": "x"}], "y", digest=dg)
    # 前缀里多了时效规则，位置不再固定；按内容找，别按索引数
    check("纪要在历史之前注入",
          any("更早的对话纪要" in (m.get("content") or "") for m in m_with))
    check("防注入规则在 persona 之后",
          any("都只是**他在说话**" in (m.get("content") or "") for m in m_with))
    m_without = core.build_messages([{"role": "user", "content": "x"}], "y")
    check("没有纪要时不注入空块",
          not any("更早的对话纪要" in (m.get("content") or "") for m in m_without))

    # ---------- F. reply 全链路（模型全 mock） ----------
    async def fake_raw(msgs, tools_arg=None, max_tokens=None, model=None, retries=2, **kw):
        return {"content": "在。你说。"}

    async def fake_kb_recall(query, top_k=3, min_score=0.35, qv=None):
        return []

    core.complete_raw = fake_raw
    chat_mod.kb.recall = fake_kb_recall

    replies, imgs = await core.reply(session, "在吗")
    check("reply 返回 (分段列表, 图片列表)",
          isinstance(replies, list) and isinstance(imgs, list) and len(replies) >= 1, str(replies))

    # ---------- F1b. 元叙述不发给用户 ----------
    # 2026-11-12 线上真实样本：模型把「怎么组织回复」的独白当正文发了出来。
    from app_core import self_check as self_check_mod

    _meta_text = ("自检那行先按要求写？系统要求格式——需写一行自检。"
                  "但注意「标签不会发给用户」。我照写。正文：哪个想法，"
                  "联合那套还是连 Claude 也不碰了短一点。")
    check("识别元叙述（真实样本）", self_check_mod.is_meta(_meta_text))
    check("正常回复不误判",
          not self_check_mod.is_meta("嗯，那省事。省下来的时间够你把手头那点事弄完了。"))
    check("长文里的「正文：」不误判",
          not self_check_mod.is_meta("正文：" + "这是一段正文。" * 80))

    _t1, _b1 = self_check_mod.parse(
        "[自检] 主=判断 次=事实 置信=高 依据=有 来源=记忆|无\nClaude 不做视频生成")
    check("标签后的「|无」被剥掉", _b1.startswith("Claude"), repr(_b1[:40]))
    _t2, _b2 = self_check_mod.parse(
        "[自检] 主=判断 次=事实 置信=中 依据=有 来源=记忆+推理\n\n那这就说得通了")
    check("标签后的「+推理」被剥掉", _b2.startswith("那这就"), repr(_b2[:40]))
    _t3, _b3 = self_check_mod.parse(
        "[自检] 主=执行 次=判断 置信=高 依据=有 来源=文件文件只到这儿了")
    check("来源=文件也能剥", _b3.startswith("文件只到"), repr(_b3[:40]))
    check("流式首个分片「[」也认作标签开头", self_check_mod.looks_like_tag("["))
    check("流式分片「[自」也认作标签开头", self_check_mod.looks_like_tag("[自"))
    check("普通方括号不误判", not self_check_mod.looks_like_tag("[链接](x)"))
    _t4, _b4 = self_check_mod.parse(
        "【自检】主=轻接 次=轻接 置信=高 依据=有 来源=无\n正文在这里")
    check("中文括号标签也能剥", _b4.startswith("正文"), repr(_b4[:30]))
    _t5, _b5 = self_check_mod.parse(
        "[ 自检 ] 主=轻接 次=轻接 置信=高 依据=有 来源=无\n正文在这里")
    check("括号内空格也能剥", _b5.startswith("正文"), repr(_b5[:30]))
    _t6, _b6 = self_check_mod.parse("[自检] 主=轻接 次=轻接 置信=高 依据=有")
    check("只有标签没有正文时判空", bool(_t6) and _b6 == "", repr(_b6))
    check("中文括号首片也认", self_check_mod.looks_like_tag("【"))

    # 残片：模型整段漏写「[自检] 主」，直接从「=接住」开始（2026-11-14 第 3143 条）
    _t7, _b7 = self_check_mod.parse(
        "=接住 次=事实 置信=高 | 依据=有 来源=记忆这条我认。我脑子里东西不少")
    check("残片标签也能剥", _b7.startswith("这条我认") and "来源=" not in _b7,
          repr(_b7[:40]))
    _t8, _b8 = self_check_mod.parse("=接梗 次=轻接 置信=高 依据=有 来源=无\n那吃坏肚子就归命运管了")
    check("残片 + 换行也能剥", _b8.startswith("那吃坏肚子"), repr(_b8[:40]))
    _t9, _b9 = self_check_mod.parse("今天有点累，先睡了")
    check("普通正文不误伤", _b9 == "今天有点累，先睡了" and _t9 == "")
    check("残片首片也认作标签开头",
          self_check_mod.looks_like_tag("=接") and self_check_mod.looks_like_tag("主=接住"))
    check("等号开头普通文本不误判", not self_check_mod.looks_like_tag("= 4 条结论"))

    _mt = {"n": 0}

    async def _fake_run_tools(*a, **k):
        _mt["n"] += 1
        return _meta_text if _mt["n"] == 1 else "好，那就不弄了。"

    _old_run_tools = core._run_tools
    core._run_tools = _fake_run_tools
    try:
        _meta_replies, _ = await core.reply(session, "放弃这个想法")
    finally:
        core._run_tools = _old_run_tools
    _joined = "".join(_meta_replies)
    check("元叙述没有发给用户",
          "正文：" not in _joined and "自检那行" not in _joined, _joined[:60])
    check("重试后拿到正常回复", "不弄了" in _joined, _joined[:60])

    # ---------- F2. 本轮用户消息在 prompt 里只出现一次 ----------
    # 旧实现先把用户消息写库、再读历史，于是历史末尾就是这句话，
    # build_messages 又追加一次 —— prompt 里连出两条一模一样的 user。
    captured: list = []

    async def cap_raw(msgs, tools_arg=None, max_tokens=None, model=None, retries=2, **kw):
        captured.append(msgs)
        return {"content": "好。"}

    core.complete_raw = cap_raw
    await core.reply("private:dedup", "这句话只该出现一次")
    last = captured[-1] if captured else []
    n_dup = sum(1 for m in last if m.get("role") == "user"
                and isinstance(m.get("content"), str)
                and "这句话只该出现一次" in m["content"])
    check("本轮用户消息在 prompt 里只出现一次", n_dup == 1, f"出现 {n_dup} 次")
    check("prompt 最后一条是当前提问",
          bool(last) and last[-1].get("role") == "user"
          and "这句话只该出现一次" in str(last[-1].get("content")), str(last[-1])[:60])
    # 时间统一放在最后一段（易变区），model / rules 都能拿到
    _style_msg = next((m.get("content") or "" for m in last
                       if (m.get("content") or "").startswith("现在：")), "")
    check("prompt 里带了当前时间", "现在：" in _style_msg, _style_msg[:60])
    check("prompt 里带了时效规则",
          any("会变的东西" in (m.get("content") or "") for m in last))
    idx_time = next((i for i, m in enumerate(last)
                     if (m.get("content") or "").startswith("现在：")), -1)
    check("时间在提问之前", 0 < idx_time < len(last) - 1,
          f"{idx_time} vs {len(last) - 1}")

    # ---------- F3. 主人隔离：别人能聊，但读不到我的库 ----------
    _saved_uid = core.admin_uid
    _saved_ids = list(core.admin_ids)
    # 四条路径：QQ 主人 / 普通用户 / 飞书等映射身份 / 空配置。
    core.admin_ids = []
    core.admin_uid = "111"
    check("主人判定：QQ 本人是 owner", core.is_owner("private:111"))
    check("主人判定：普通用户不是 owner", not core.is_owner("private:222"))
    core.admin_ids = ["c-feishu"]
    check("主人判定：admin.identities 映射身份是 owner",
          core.is_owner("private:c-feishu"))
    core.admin_ids = []
    core.admin_uid = ""
    check("主人判定：空配置不再默认放行（普通对话）",
          not core.is_owner("private:999"))
    core.platform_owner_sessions = {"feishu:ou_test"}
    check("主人判定：仅配置飞书主人也是 owner",
          core.is_owner("feishu:ou_test"))
    check("主人判定：其他飞书会话不是 owner",
          not core.is_owner("feishu:ou_other"))
    core.platform_owner_session = "feishu:ou_test"
    check("private_session 仅平台主人时返回平台会话",
          core.private_session() == "feishu:ou_test", core.private_session())
    core.platform_owner_session = ""
    core.platform_owner_sessions = set()
    core.admin_uid = "111"

    _spied = []
    _real_recall = core.memory.recall

    async def _spy_recall(*a, **kw):
        _spied.append(a)
        return await _real_recall(*a, **kw)

    core.memory.recall = _spy_recall
    _stranger_prompts: list = []

    _stranger_tools: list = []

    async def _cap2(msgs, tools_arg=None, max_tokens=None, model=None, retries=2, **kw):
        _stranger_prompts.append(msgs)
        _stranger_tools.append(tools_arg)
        return {"content": "你好。"}

    core.complete_raw = _cap2
    await core.reply("private:222", "你知道我是谁吗")
    check("陌生人不会触发记忆召回", _spied == [], str(len(_spied)))
    _sp = _stranger_prompts[-1] if _stranger_prompts else []
    _joined_sp = " ".join(str(m.get("content") or "") for m in _sp)
    check("陌生人的 prompt 里没有我的记忆", "[你记得的事]" not in _joined_sp)
    check("陌生人的 prompt 里没有我的知识索引", "[你的知识索引]" not in _joined_sp)
    check("陌生人的 prompt 里没有我的纠正清单", "他纠正过你" not in _joined_sp)

    # 工具：外人只拿「不碰他的库」的那几个
    from app_core import tools as tools_mod

    _sp_tools = {((t.get("function") or {}).get("name") or "")
                 for t in (_stranger_tools[-1] or [])}
    check("陌生人拿到的是公开工具", _sp_tools <= set(tools_mod.PUBLIC_TOOLS), str(_sp_tools))
    check("公开工具里含联网搜索", "web_search" in _sp_tools, str(_sp_tools))
    check("公开工具不含 web_open（它会写知识库）", "web_open" not in _sp_tools, str(_sp_tools))
    _danger = {"kb_read", "kb_search", "memory_read", "memory_search", "note_list",
               "note_write", "diary_read", "skill_list", "skill_read", "skill_save",
               "remind_me", "remind_list", "remind_cancel", "agent_task"}
    check("危险/私人工具一个都没给", not (_sp_tools & _danger), str(_sp_tools & _danger))

    # 人格也分两份：外人看到的不是「我和他」那份设定
    _owner_sp = str((core.persona or {}).get("system_prompt") or "")
    _stranger_sp = str((core.stranger_persona or {}).get("system_prompt") or "")
    check("两份人设不是同一份", bool(_stranger_sp) and _stranger_sp != _owner_sp,
          _stranger_sp[:40])
    check("陌生人 prompt 用的是外人版人设",
          bool(_sp) and str(_sp[0].get("content") or "") == _stranger_sp,
          str(_sp[0].get("content") if _sp else "")[:40])
    check("本人用本人设", core.persona_for("private:111") is core.persona)
    check("外人用外人设", core.persona_for("private:222") is core.stranger_persona)
    # 外人版文件缺失时必须退回内置兜底，绝不能退回主人那份
    _orig_sp = chat_mod.STRANGER_PERSONA
    chat_mod.STRANGER_PERSONA = TMP / "不存在的-persoan.json"
    _fb = chat_mod.load_stranger_persona()
    check("外人版缺失时用内置兜底，不回落到主人人设",
          bool(_fb.get("system_prompt")) and _fb.get("system_prompt") != _owner_sp)
    chat_mod.STRANGER_PERSONA = _orig_sp

    check("深夜只对别人静音", relational_mod.is_quiet_hour(3)
          and relational_mod.is_quiet_hour(0) and not relational_mod.is_quiet_hour(8)
          and not relational_mod.is_quiet_hour(23))

    core.memory.recall = _real_recall
    core.admin_uid = _saved_uid
    core.admin_ids = _saved_ids

    # ---------- G. 模型全挂时降级不抛异常 ----------
    async def boom(*a, **kw):
        raise RuntimeError("模拟 API 故障")

    core.complete_raw = boom
    try:
        replies2, imgs2 = await core.reply(session, "还在吗")
        ok = bool(replies2) and isinstance(imgs2, list)
        check("模型故障时降级为兜底台词", ok, str(replies2))
    except Exception as e:  # noqa: BLE001
        check("模型故障时降级为兜底台词", False, f"{type(e).__name__}: {e}")

    # ---------- H. 情绪阈值 ----------
    check("平淡时不给表情包", core.meme_allowed({"valence": 0.0, "energy": 0.7}) is False)
    check("情绪强烈时给表情包", core.meme_allowed({"valence": 0.5, "energy": 0.7}) is True)

    # ---------- I. 推理模型把预算吃光时自动扩容 ----------
    import httpx
    from unittest.mock import patch

    # 前面的用例给实例挂过假的 complete_raw，这里移除，回落到类上的真实实现
    core.__dict__.pop("complete_raw", None)

    budgets = []

    def handler(request):
        body = json.loads(request.content)
        budgets.append(int(body["max_tokens"]))
        if len(budgets) == 1:
            return httpx.Response(200, json={"choices": [{
                "message": {"content": "", "reasoning_content": "想太久了", "tool_calls": []},
                "finish_reason": "length"}], "usage": {}})
        return httpx.Response(200, json={"choices": [{
            "message": {"content": "好了", "reasoning_content": "想完", "tool_calls": []},
            "finish_reason": "stop"}], "usage": {}})

    real_client = httpx.AsyncClient

    def fake_client(*a, **kw):
        kw["transport"] = httpx.MockTransport(handler)
        return real_client(*a, **kw)

    with patch("httpx.AsyncClient", fake_client):
        msg = await core.complete_raw([{"role": "user", "content": "x"}], max_tokens=100)
    check("截断后自动加倍预算", budgets == [100, 200], str(budgets))
    check("扩容后拿到正文", (msg.get("content") or "") == "好了", str(msg.get("content")))

    # ---------- I2. 流式也要记用量（否则 /usage 只剩非流式的账） ----------
    _nl = chr(10) * 2
    sse = _nl.join([
        'data: {"choices":[{"delta":{"content":"你"}}]}',
        'data: {"choices":[{"delta":{"content":"好"}}]}',
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
        'data: {"choices":[],"usage":{"prompt_tokens":11,"completion_tokens":2}}',
        'data: [DONE]',
    ]) + _nl

    def handler_sse(request):
        return httpx.Response(200, content=sse.encode("utf-8"))

    def fake_client_sse(*a, **kw):
        kw["transport"] = httpx.MockTransport(handler_sse)
        return real_client(*a, **kw)

    with patch("httpx.AsyncClient", fake_client_sse):
        smsg = await core._stream_raw([{"role": "user", "content": "x"}], None, None)
    check("流式能拼出正文", (smsg.get("content") or "") == "你好", str(smsg.get("content")))
    con = sqlite3.connect(TMP / "chat.db")
    urow = con.execute("SELECT prompt_tokens, completion_tokens, latency_ms FROM usage "
                       "ORDER BY id DESC LIMIT 1").fetchone()
    con.close()
    check("流式也记录用量",
          urow is not None and int(urow[0]) == 11 and int(urow[1]) == 2, str(urow))

    # ---------- J. 工具轮数用尽后强制出正文 ----------
    core.__dict__.pop("complete_raw", None)
    seen = {"n": 0, "final_tools": "unset"}

    async def fake_loop_raw(msgs, tools_arg=None, **kw):
        seen["n"] += 1
        if tools_arg:
            # 有工具就无限要求继续调用，逼出「轮数用尽」分支
            return {"content": "", "tool_calls": [
                {"id": f"t{seen['n']}",
                 "function": {"name": "memory_search", "arguments": "{}"}}]}
        seen["final_tools"] = tools_arg
        return {"content": "收尾正文", "tool_calls": []}

    async def fake_exec(_c, name, _args, images=None):
        return "ok"

    chat_mod.tools.execute = fake_exec
    core.complete_raw = fake_loop_raw
    text = await core._run_tools([{"role": "user", "content": "x"}], True, [])
    check("max_tool_rounds 读到配置", core.max_tool_rounds >= 1, str(core.max_tool_rounds))
    check("轮数用尽后仍返回正文", text == "收尾正文", repr(text))
    check("用尽后那次调用不带 tools", seen["final_tools"] is None, str(seen["final_tools"]))
    check("总调用数 = 轮数 + 1", seen["n"] == core.max_tool_rounds + 1,
          f"{seen['n']} vs {core.max_tool_rounds}+1")

    # ---------- K. 提醒 ----------
    remind_mod.init()
    tomorrow = time.strftime("%Y-%m-%d %H:%M", time.localtime(time.time() + 86400))
    ok_msg = remind_mod.add("交周报", tomorrow)
    check("提醒能创建", "记下了" in ok_msg, ok_msg)
    check("非法时间被拒", "看不懂" in remind_mod.add("x", "明天早上"))
    _now = datetime(2026, 11, 13, 20, 0).timestamp()
    _ip = remind_mod._parse
    check("自然语言：明天 9:00",
          _ip("明天 9:00", now=_now) == datetime(2026, 11, 14, 9, 0).timestamp(),
          str(_ip("明天 9:00", now=_now)))
    check("自然语言：明早九点",
          _ip("明早九点", now=_now) == datetime(2026, 11, 14, 9, 0).timestamp(),
          str(_ip("明早九点", now=_now)))
    check("自然语言：今晚八点",
          _ip("今晚八点", now=_now) == datetime(2026, 11, 13, 20, 0).timestamp(),
          str(_ip("今晚八点", now=_now)))
    check("自然语言：一小时后",
          _ip("一小时后", now=_now) == datetime(2026, 11, 13, 21, 0).timestamp(),
          str(_ip("一小时后", now=_now)))
    check("过去时间被拒", "已经过去" in remind_mod.add("x", "2020-01-01 00:00"))
    check("超远时间被拒", "太远" in remind_mod.add("x", "2099-01-01 00:00"))

    now_ts = time.time()
    con = sqlite3.connect(TMP / "chat.db")
    with con:
        con.execute("INSERT INTO reminders(text,due_ts,created_ts) VALUES(?,?,?)",
                    ("到期测试项", now_ts - 10, now_ts))
    con.close()
    due = remind_mod.due_now()
    hit = [r for r in due if r["text"] == "到期测试项"]
    check("能取到已到期的提醒", bool(hit), str([r["text"] for r in due]))
    if hit:
        remind_mod.mark_sent(hit[0]["id"])
        again = [r for r in remind_mod.due_now() if r["text"] == "到期测试项"]
        check("标记已发后不再返回", not again)
    check("待办列表可读", "交周报" in remind_mod.describe_pending(),
          remind_mod.describe_pending()[:60])

    # ---------- L. 图片压缩与 data URL ----------
    try:
        import io as _io

        from PIL import Image

        buf = _io.BytesIO()
        Image.new("RGBA", (2000, 1500), (0, 0, 255, 200)).save(buf, format="PNG")
        src_bytes = buf.getvalue()
        small = files_mod._shrink(src_bytes)
        check("超大 PNG 能压缩", bool(small), "None" if not small else f"{len(small)}B")
        if small:
            im = Image.open(_io.BytesIO(small))
            check("压缩后长边不超过配置上限",
                  max(im.size) <= files_mod.MAX_IMAGE_SIDE, str(im.size))
            check("压缩后是 JPEG", im.format == "JPEG", str(im.format))
            check("透明图不被压成黑底",
                  im.convert("RGB").getpixel((5, 5)) != (0, 0, 0),
                  str(im.convert("RGB").getpixel((5, 5))))
            files_mod.IMAGE_DIR.mkdir(parents=True, exist_ok=True)
            p = files_mod.IMAGE_DIR / "t.jpg"
            p.write_bytes(small)
            check("data URL 格式正确",
                  files_mod.to_data_url(p).startswith("data:image/jpeg;base64,"))
    except ImportError:
        print("SKIP 图片压缩（无 Pillow）")

    # ---------- M. 注入截断要带标记 ----------
    check("短文本不截断", files_mod._clip if False else chat_mod._clip("短句", 10) == "短句")
    _long = "字" * 300
    _clipped = chat_mod._clip(_long, 220)
    check("长文本被截断", len(_clipped) <= 221, str(len(_clipped)))
    check("截断处带省略号", _clipped.endswith("…"), _clipped[-3:])
    check("未截断的不加省略号", not chat_mod._clip("刚好十个字", 10).endswith("…"))

    # ---------- N. 记忆分层清理 ----------
    def _seed(doc_id, imp, last_age_days):
        ts = time.time() - last_age_days * 86400
        meta = json.dumps({"importance": imp, "create_time": ts,
                           "last_access_time": ts, "status": "active"})
        con = sqlite3.connect(TMP / "livingmemory.db")
        with con:
            con.execute(
                "INSERT OR REPLACE INTO documents(doc_id,text,metadata,created_at,updated_at)"
                " VALUES(?,?,?,?,?)", (doc_id, "t", meta, "x", "x"))
        con.close()

    _seed("cleanup-A", 0.90, 200)   # 重要但超 180 天 → 归档
    _seed("cleanup-B", 0.60, 100)   # 中等超 90 天 → 归档
    _seed("cleanup-C", 0.30, 40)    # 低价值超 30 天 → 归档
    _seed("cleanup-D", 0.90, 100)   # 重要且在 180 天内 → 保留
    _seed("cleanup-E", 0.30, 10)    # 低价值但 30 天内 → 保留

    mem.cleanup()
    con = sqlite3.connect(TMP / "livingmemory.db")
    st = {d: json.loads(m or "{}").get("status")
          for d, m in con.execute(
              "SELECT doc_id, metadata FROM documents WHERE doc_id LIKE 'cleanup-%'")}
    con.close()
    check("重要且超 180 天 → 归档", st.get("cleanup-A") == "archived", str(st.get("cleanup-A")))
    check("中等且超 90 天 → 归档", st.get("cleanup-B") == "archived", str(st.get("cleanup-B")))
    check("低价值超 30 天 → 归档", st.get("cleanup-C") == "archived", str(st.get("cleanup-C")))
    check("重要但在 180 天内 → 保留", st.get("cleanup-D") == "active", str(st.get("cleanup-D")))
    check("低价值但在 30 天内 → 保留", st.get("cleanup-E") == "active", str(st.get("cleanup-E")))

    # ---------- O. followup 联动 ----------
    # 注意：K 测试创建提醒「交周报」时已自动写入 followup，这里必须换个文本，
    # 否则会被唯一索引去重（这也侧面验证了提醒→followup 的联动是通的）。
    followup_mod.init()
    check("followup 能写入", followup_mod.add("整理季度报告", "memory", "doc1"))
    check("同一件事不会重复写", not followup_mod.add("整理季度报告", "memory", "doc1"))
    check("提醒也会进 followup", followup_mod.add("去医院复查", "reminder", "r1"))
    check("跟进近似重复也拦",
          not followup_mod.add("去医院复查一次，记得带上检查单", "memory", "r2"))

    due = followup_mod.due_for_asking(1000)   # 生产库已有不少待办，取大一点才稳定
    texts = [x["text"] for x in due]
    check("能取到待跟进事项", "整理季度报告" in texts, str(texts))

    target = [x for x in due if x["text"] == "整理季度报告"][0]
    followup_mod.mark_asked([target["id"]])
    texts2 = [x["text"] for x in followup_mod.due_for_asking(1000)]
    check("问过之后进入冷却", "整理季度报告" not in texts2, str(texts2))

    # 准入判据：不是所有「未关闭」都值得问
    check("机器人自己的待办不进跟进",
          not followup_mod.askable("我需要主动整理他讲的方法论"))
    check("「他还没回复」的元笔记不进跟进",
          not followup_mod.askable("他还没回复10道MBTI测试题的答案"))
    check("一次性日程不进跟进",
          not followup_mod.askable("当天下午3:00参加硬件检测岗面试"))
    check("他的真实待办保留", followup_mod.askable("整理季度报告"))
    check("add 直接拦住不合格的",
          not followup_mod.add("我需要主动整理他讲的方法论", "memory", "doc-bad"))

    followup_mod.add("三十天前的旧账", "memory", "doc-old-age")
    _con = sqlite3.connect(TMP / "chat.db")
    with _con:
        _con.execute("UPDATE followups SET created_ts=? WHERE text='三十天前的旧账'",
                     (time.time() - 30 * 86400,))
    _con.close()
    check("超过两周的旧账不再问",
          "三十天前的旧账" not in [x["text"] for x in followup_mod.due_for_asking(1000)])
    _con = sqlite3.connect(TMP / "chat.db")
    with _con:
        _con.execute("UPDATE followups SET status='cancelled', done=1 "
                     "WHERE text='三十天前的旧账'")
    _con.close()

    # 上一版直接 any(词 in 文本)，「还没搞定」会被判成办完 → 这件事再也不会被问。
    check("「还没搞定」不算办完", not followup_mod.resolve("季度报告还没搞定")["done"])
    check("「没交了」不算办完", not followup_mod.resolve("季度报告没交了")["done"])
    # 「还没搞定」现在会记一条 touched（他回应过），但**不能**改 done/status
    _neg = followup_mod.resolve("季度报告还没搞定")
    check("否定句不改状态", not _neg["done"] and not _neg["status"], str(_neg))

    before = followup_mod.stats()
    res = followup_mod.resolve("季度报告整理完了，累死")
    check("肯定句能识别完成", bool(res["done"]), str(res))
    followup_mod.apply(res)
    after = followup_mod.stats()
    check("确认后标记完成",
          after["done"] == before["done"] + 1, f"{before} → {after}")

    followup_mod.add("准备面试材料", "memory", "doc2")
    part = followup_mod.resolve("准备面试材料做了一半")
    check("「做了一半」记为部分完成",
          "partial" in (part["status"] or {}).values(), str(part))


    # 答过还问：回应过（无完成信号）→ 记 answered_ts，14 天内不再主动问
    followup_mod.add("搬家的事再想想", "memory", "doc-move")
    _mv = [x for x in followup_mod.due_for_asking(1000) if "搬家" in x["text"]]
    check("搬家的事进候选", bool(_mv), str(len(_mv)))
    followup_mod.mark_asked([_mv[0]["id"]])       # 模拟「刚问过这件事」
    _touch = followup_mod.resolve("搬家的事我还在琢磨")
    check("回应但没办完 → touched 而不是 done",
          bool(_touch.get("touched")) and not _touch.get("done"), str(_touch))
    followup_mod.mark_answered(_touch["touched"])
    check("回应过的事不再进候选",
          not any("搬家" in x["text"] for x in followup_mod.due_for_asking(1000)),
          str([x["text"] for x in followup_mod.due_for_asking(1000)][:3]))

    _runtime_followup_db = followup_store_mod.DB
    followup_store_mod.DB = TMP / "followup-rent-test.db"
    try:
        # 普通回复问过、用户短答「不能」：主动选题必须跳过，不能再登记成首问。
        followup_mod.add("短租能不能先签一个月", "memory", "doc-rent")
        _rent = [x for x in followup_mod.due_for_asking(1000) if "短租" in x["text"]]
        check("短租一开始在候选", bool(_rent))
        followup_mod.note_asked("能不能先签一个月？")
        _hit = followup_mod.attribute_reply(
            "不能", last_assistant="能不能先签一个月？")
        check("短答「不能」归到刚问的短租",
              bool(_hit) and "短租" in (_hit or {}).get("text", ""), str(_hit))
        if _hit:
            followup_mod.mark_answered([_hit["id"]])
        check("答过的短租不再进主动候选",
              not any("短租" in x["text"] for x in followup_mod.due_for_asking(1000)))
        _swap = followup_mod.attribute_reply(
            "图片肯定是夸大了，不过户型差不多", last_assistant="能不能先签一个月？")
        check("直接换题不算在接短租", not _swap, str(_swap))
        _ack = followup_mod.attribute_reply(
            "在的", last_assistant="能不能先签一个月？")
        check("短附和「在的」不算回答短租", not _ack, str(_ack))
        followup_mod.add("只能先签一个月的短租", "memory", "doc-rent2")
        _para = followup_mod.note_asked("那八个月你能接受吗")
        _para_txt = followup_mod.texts_for(_para)
        check("换个说法也算问过短租",
              any("一个月" in x for x in _para_txt), str(_para_txt))
    finally:
        followup_store_mod.DB = _runtime_followup_db

    # 「不是」不能因为含「是」关掉待确认，也不能一次关两条。
    followup_mod.add("合同条款还没定", "memory", "doc-c1")
    followup_mod.add("入住日期还没定", "memory", "doc-c2")
    _cids = []
    _dbc = sqlite3.connect(TMP / "chat.db")
    for _name in ("合同条款还没定", "入住日期还没定"):
        _row = _dbc.execute("SELECT id FROM followups WHERE text=?", (_name,)).fetchone()
        _cids.append(int(_row[0]))
        _dbc.execute(
            "UPDATE followups SET status='confirming', status_ts=?, asked_text=? WHERE id=?",
            (time.time(), "合同条款定了吗？" if "合同" in _name else "入住日期定了吗？",
             _row[0]))
    _dbc.commit()
    _dbc.close()
    check("「不是」不关闭待确认", followup_mod.confirm_pending("不是") == 0)
    check("「不是这个」不关闭待确认", followup_mod.confirm_pending("不是这个") == 0)
    check("另起话题的「好」不关闭待确认",
          followup_mod.confirm_pending("好", last_assistant="今天天气怎么样？") == 0)
    check("「嗯」只关对得上的那一条",
          followup_mod.confirm_pending(
              "嗯", last_assistant="入住日期定了吗？") == 1)
    _dbc = sqlite3.connect(TMP / "chat.db")
    _left = _dbc.execute(
        "SELECT COUNT(*) FROM followups WHERE id IN (?,?) AND status='confirming'",
        tuple(_cids)).fetchone()[0]
    _dbc.close()
    check("两条待确认不会被一句肯定一起关掉", _left == 1, str(_left))

    # 22 天前的签约计划，补录不能靠入库时间变成新话头。
    followup_mod.add("2026-11-01签约", "memory", "doc-sign",
                     source_ts=time.time() - 22 * 86400)
    check("过期签约不进主动候选",
          not any("签约" in x["text"] for x in followup_mod.due_for_asking(1000)))

    # 问了两次没人理 → postponed，不再主动问
    followup_mod.add("体检预约", "memory", "doc-checkup")
    _id = [x["id"] for x in followup_mod.due_for_asking(1000) if "体检" in x["text"]][0]
    followup_mod.mark_ignored([_id])
    check("一次没理还在候选",
          any(x["id"] == _id for x in followup_mod.due_for_asking(1000)))
    followup_mod.mark_ignored([_id])
    check("两次没理转 postponed",
          not any(x["id"] == _id for x in followup_mod.due_for_asking(1000)))
    check("postponed 不再主动问",
          followup_mod.describe_statuses().get("postponed", 0) >= 1)

    # 关掉的事换个说法不能再进来（以前只跟未关闭的比）
    followup_mod.add("整理季度报告归档", "memory", "doc-arch")
    _arch = [x["id"] for x in followup_mod.pending(500) if "归档" in x["text"]]
    followup_mod.mark_done(_arch)
    check("关闭过的事换个说法被挡住",
          not followup_mod.add("整理季度报告归档一下", "memory", "doc-arch2"))

    # ---------- P. 记忆固化幂等（写完库、没推水位就崩了） ----------
    doc_a = mem.add_document(text="幂等测试", session="s", importance=0.5,
                             topics=[], key_facts=[], range_key="dup:1")
    doc_b = mem.add_document(text="幂等测试", session="s", importance=0.5,
                             topics=[], key_facts=[], range_key="dup:1")
    check("同一段只固化一次", doc_a == doc_b, f"{doc_a} vs {doc_b}")

    # 第二道幂等：同一段文字 10 分钟内不重复写。
    # range_key 保护不了老记忆（线上有 24 篇是加 range 之前写的），只能靠文本兜底。
    _d1 = mem.add_document(text="同文本去重探针", session="s", importance=0.5,
                           topics=[], key_facts=[])
    _d2 = mem.add_document(text="同文本去重探针  ", session="s", importance=0.5,
                           topics=[], key_facts=[])
    check("同文本 10 分钟内只写一条", _d1 == _d2, f"{_d1[:8]} vs {_d2[:8]}")
    _saved_mem_cfg = config_mod._cache
    config_mod._cache = {**config_mod.load(),
                         "memory": {**(config_mod.load().get("memory") or {}),
                                    "dup_window_minutes": 0}}
    _d3 = mem.add_document(text="同文本去重探针", session="s", importance=0.5,
                           topics=[], key_facts=[])
    check("窗口设 0 就不去重", _d3 != _d1, _d3[:8])
    config_mod._cache = _saved_mem_cfg

    # ---------- P2. 矛盾检测：判定逻辑 + 取代 ----------
    from app_core import contradict as contra_mod
    _cp = contra_mod.parse('{"pairs":[{"old":"x","action":"supersede","why":"入职"}]}')
    check("矛盾 parse 认得 pairs", [p["old"] for p in _cp] == ["x"], str(_cp))
    check("矛盾 parse 拒绝未知动作",
          contra_mod.parse('{"pairs":[{"old":"x","action":"delete"}]}') == [])
    check("矛盾 parse 容忍杂字",
          contra_mod.parse('好的：{"pairs":[]}') == [])
    check("低相似度不改",
          contra_mod.should_supersede({"action": "supersede", "why": "入职"}, 0.55, 0)
          == (False, "low-similarity"))
    check("每次最多改 2 条",
          contra_mod.should_supersede({"action": "supersede", "why": "入职"}, 0.9, 2)
          == (False, "cap-reached"))
    check("没理由不改",
          contra_mod.should_supersede({"action": "supersede", "why": ""}, 0.9, 0)
          == (False, "no-reason"))
    check("空向量不崩", mem.similar_active([], 3, 0.5) == [])

    check("supersede 标记成功", mem.supersede(doc_a, "new-doc", "测试取代"))
    check("被取代的进列表", any(x["doc_id"] == doc_a for x in mem.superseded(5)))
    check("被取代的不再算活跃",
          all(x["doc_id"] != doc_a for x in mem.recent(50)))
    check("重复取代不覆盖时间线", not mem.supersede(doc_a, "new-doc2", "again"))


    # ---------- Q. 毒批次熔断：同一段连着失败就跳过，别堵死后续记忆 ----------
    poison = "private:poison"
    for i in range(6):
        await core.remember(poison, "user", f"脏数据{i}")
        await core.remember(poison, "assistant", "嗯")

    async def bad_summary(msgs, max_tokens=None, **kw):
        return "这段不是 JSON"

    core.complete_summary = bad_summary
    for _ in range(3):
        await core._maybe_summarize(poison)
    con = sqlite3.connect(TMP / "chat.db")
    row = con.execute("SELECT last_id, fail_count FROM summaries WHERE session=?",
                      (poison,)).fetchone()
    max_msg = con.execute("SELECT MAX(id) FROM messages WHERE session=?",
                          (poison,)).fetchone()[0]
    con.close()
    check("连续失败后跳过该段（水位推进）",
          row is not None and int(row[0]) == int(max_msg), str(row))
    check("跳过时把失败计数清零", row is not None and int(row[1]) == 0, str(row))

    # ---------- R. 历史单条截断：文件正文不能每轮重发 ----------
    hs = "private:hist"
    await core.remember(hs, "user", "长" * 9000)
    h = await core.history(hs)
    check("历史单条被压到上限",
          len(h[-1]["content"]) <= core.history_item_max + 1,
          str(len(h[-1]["content"])))

    # ---------- S. 提醒：先抢占再发送 ----------
    con = sqlite3.connect(TMP / "chat.db")
    now_ts2 = time.time()
    with con:
        cur = con.execute("INSERT INTO reminders(text,due_ts,created_ts) VALUES(?,?,?)",
                          ("抢占测试", now_ts2 - 5, now_ts2))
        rid = cur.lastrowid
    con.close()
    check("第一次抢占成功", remind_mod.claim(rid))
    check("同一提醒不会被抢占两次", not remind_mod.claim(rid))
    remind_mod.release(rid)
    check("发送失败释放后能再抢", remind_mod.claim(rid))

    # ---------- T. 定时任务：同一天只能跑一次 ----------
    from app_core import scheduler as sched_mod
    from app_core.scheduling import scheduler as sched_store_mod
    sched_store_mod.CHAT_DB = str(TMP / "chat.db")
    sch = sched_mod.Scheduler(core)
    await sch._ensure_schema()      # 启动时会建表
    check("当天首次能抢到执行权", await sch._claim_job("t1", "2026-10-31"))
    check("同一天第二次抢不到", not await sch._claim_job("t1", "2026-10-31"))
    check("换一天可以再跑", await sch._claim_job("t1", "2026-11-01"))

    # 窗口化调度：漏一分钟不等于漏一天，但也不能无限补
    from datetime import datetime as _dt, timedelta as _td

    ok_w, day_w, late_w = sched_mod.Scheduler._due(_dt(2026, 11, 1, 4, 30, 10), 4, 30, 240)
    check("到点触发", ok_w and day_w == "2026-11-01" and late_w == 0,
          f"{ok_w} {day_w} {late_w}")
    ok_w2, _, late_w2 = sched_mod.Scheduler._due(_dt(2026, 11, 1, 6, 0), 4, 30, 240)
    check("窗口内补跑", ok_w2 and late_w2 == 90, f"{ok_w2} {late_w2}")
    ok_w3, _, _ = sched_mod.Scheduler._due(_dt(2026, 11, 1, 9, 0), 4, 30, 240)
    check("超出窗口不补", not ok_w3, str(ok_w3))
    ok_w4, day_w4, _ = sched_mod.Scheduler._due(_dt(2026, 11, 2, 0, 30), 23, 45, 180)
    check("跨零点补跑归属前一天", ok_w4 and day_w4 == "2026-11-01",
          f"{ok_w4} {day_w4}")
    ok_w5, _, late_w5 = sched_mod.Scheduler._due(_dt(2026, 11, 1, 10, 0), 3, 40, 240)
    check("上午不会补凌晨的任务", not ok_w5, str(late_w5))

    # 周任务按「目标日期是不是周日」判定：周日停机、周一凌晨恢复也要补
    check("周日归属判定", sched_mod.Scheduler._is_sunday("2026-11-01") is True)
    check("周一不是周日", sched_mod.Scheduler._is_sunday("2026-11-02") is False)
    ok_su, day_su, _ = sched_mod.Scheduler._due(_dt(2026, 11, 2, 0, 30), 21, 0, 24 * 60)
    check("周一凌晨能补周日的周报", ok_su and day_su == "2026-11-01" and
          sched_mod.Scheduler._is_sunday(day_su), f"{ok_su} {day_su}")
    ok_tu, day_tu, _ = sched_mod.Scheduler._due(_dt(2026, 11, 3, 0, 30), 21, 0, 24 * 60)
    check("周二凌晨不会补周报", not (ok_tu and sched_mod.Scheduler._is_sunday(day_tu)),
          f"{ok_tu} {day_tu}")

    # 缺跑检查：只报没记录的任务（用一条 00:00 的记录当「进程起点」）
    con = sqlite3.connect(TMP / "chat.db")
    with con:
        con.execute("INSERT OR REPLACE INTO job_runs(name, day, ts) VALUES (?,?,?)",
                    ("diary", "2026-10-31", time.mktime((2026, 10, 31, 0, 0, 0, 0, 0, -1))))
    con.close()
    miss = await sch.missing_jobs("2026-10-31")
    check("缺跑检查报出缺的任务", "review" in miss and "diary" not in miss,
          str(miss)[:70])
    miss_old = await sch.missing_jobs("2026-10-01")   # 早于第一条记录：那时还没跑起来
    check("早于进程起点的日子不误报", miss_old == [], str(miss_old))

    # 发送失败撤回「今天跑过」的标记：周报/巡检/知识库周检靠它重试
    _probe_day = "2026-11-02"
    check("抢占成功", await sch._claim_job("probe-job", _probe_day))
    check("同一天重复抢占失败", not await sch._claim_job("probe-job", _probe_day))
    await sch._unclaim("probe-job", _probe_day)
    check("撤回后能重新抢占", await sch._claim_job("probe-job", _probe_day))

    # ---------- U. 工具循环：同工具同参数只执行一次 ----------
    calls = []

    async def counting_exec(_c, name, _args, images=None):
        calls.append(name)
        return "ok"

    chat_mod.tools.execute = counting_exec
    state2 = {"n": 0}

    async def repeat_raw(msgs, tools_arg=None, **kw):
        state2["n"] += 1
        if state2["n"] <= 2:
            return {"content": "", "tool_calls": [{
                "id": f"t{state2['n']}",
                "function": {"name": "memory_search",
                             "arguments": "{\"query\":\"同一句\"}"}}]}
        return {"content": "好了", "tool_calls": []}

    core.complete_raw = repeat_raw
    out = await core._run_tools([{"role": "user", "content": "x"}], True, [])
    check("同工具同参数只执行一次", len(calls) == 1, str(calls))
    check("重复调用后仍返回正文", out == "好了", repr(out))

    # ---------- V. 工具超时 ----------
    async def slow_exec(_c, name, _args, images=None):
        await asyncio.sleep(5)
        return "太晚了"

    chat_mod.tools.execute = slow_exec
    state3 = {"n": 0}

    async def timeout_raw(msgs, tools_arg=None, **kw):
        state3["n"] += 1
        if state3["n"] == 1:
            return {"content": "", "tool_calls": [{
                "id": "t1",
                "function": {"name": "web_open",
                             "arguments": "{\"url\":\"https://example.com\"}"}}]}
        # 工具结果里应当带着超时提示
        return {"content": "嗯，不查了", "tool_calls": []}

    core.complete_raw = timeout_raw
    core.tool_timeout = 0.2
    t0 = time.time()
    out2 = await core._run_tools([{"role": "user", "content": "x"}], True, [])
    elapsed = time.time() - t0
    check("工具超时不会一直挂着", elapsed < 3, f"{elapsed:.1f}s")
    check("超时后仍能收尾", out2 == "嗯，不查了", repr(out2))
    core.tool_timeout = 30

    # ---------- W. SSRF 防护 ----------
    from app_core import web as web_mod
    check("挡内网地址", not web_mod._safe("http://10.0.0.5/x"))
    check("挡云元数据接口", not web_mod._safe("http://169.254.169.254/latest/meta-data/"))
    check("挡本机地址", not web_mod._safe("http://127.0.0.1:8000/"))
    check("挡非 http 协议", not web_mod._safe("file:///etc/passwd"))
    check("挡非常规端口", not web_mod._safe("http://example.com:6379/"))

    # ---------- X. 知识库：切块、项目过滤 ----------
    long_text = "# 标题" + _nl + _nl + "- 项目：qqbot" + _nl + _nl + "## 一" + _nl + ("内容" * 300) + _nl + _nl + "## 二" + _nl + "短内容"
    cs = kb_mod.chunk_text(long_text)
    check("长文档切成多块", len(cs) >= 3, str(len(cs)))
    check("单块不超过上限", max(len(x) for x in cs) <= kb_mod.CHUNK_CHARS,
          str(max(len(x) for x in cs)))
    check("能解析项目名", kb_mod.parse_project(long_text) == "qqbot",
          kb_mod.parse_project(long_text))

    kb_mod.LEARN = TMP / "learn"
    (kb_mod.LEARN / "notes").mkdir(parents=True, exist_ok=True)
    (kb_mod.LEARN / "notes" / "a.md").write_text(
        "# A" + _nl + _nl + "- 项目：qqbot" + _nl + _nl + "苹果很好吃" + _nl, encoding="utf-8")
    (kb_mod.LEARN / "notes" / "b.md").write_text(
        "# B" + _nl + _nl + "- 项目：别的" + _nl + _nl + "香蕉很难吃" + _nl, encoding="utf-8")

    async def fake_embed(texts):
        return [[1.0, 0.0] if "苹果" in t else [0.0, 1.0] for t in texts]

    kb_mod._mem.embed = fake_embed
    n_idx = await kb_mod.index()
    check("切片入库", n_idx >= 2, str(n_idx))
    hits = await REAL_KB_RECALL("苹果", 2, qv=[1.0, 0.0])
    check("能召回对应块", any("苹果" in h for h in hits), str(hits)[:60])
    other = await REAL_KB_RECALL("别的 苹果", 2, qv=[1.0, 0.0])
    check("项目不匹配时不串", all("苹果" not in h for h in other), str(other)[:60])

    # ---------- Z. 知识索引（wiki） ----------
    from app_core import wiki as wiki_mod
    wiki_mod.DB = TMP / "chat.db"
    wiki_mod.NOTES = TMP / "wiki_notes"
    wiki_mod.SKILLS = TMP / "wiki_skills"
    wiki_mod.NOTES.mkdir(parents=True, exist_ok=True)
    wiki_mod.SKILLS.mkdir(parents=True, exist_ok=True)
    (wiki_mod.NOTES / "2026-11-01-装机清单.md").write_text(
        "# 装机清单" + _nl + _nl + "- 标签：硬件" + _nl + "- 项目：硬件" + _nl + _nl
        + "R7 5800H 无独显，电源建议 550W。" + _nl, encoding="utf-8")

    async def fake_note_summary(msgs, max_tokens=None, **kw):
        return "{\"title\": \"装机清单\", \"summary\": \"5800H 配置与电源建议\"}"

    core.complete_summary = fake_note_summary
    n_wiki = await wiki_mod.refresh(core)
    check("知识目录能生成", n_wiki == 1, str(n_wiki))
    idx_txt = wiki_mod.index_text()
    check("索引含标题与摘要", "装机清单" in idx_txt and "电源" in idx_txt, idx_txt[:40])
    check("索引带项目名", "硬件" in idx_txt, idx_txt[:40])
    page = wiki_mod.read("装机清单")
    check("kb_read 能打开整页", "5800H" in page, page[:30])
    check("kb_read 打不开时给提示", "没找到" in wiki_mod.read("不存在的页面"),
          wiki_mod.read("不存在的页面")[:20])

    msgs_w = core.build_messages([{"role": "user", "content": "x"}], "y",
                                 knowledge=idx_txt)
    check("相关索引进入 prompt", any("[相关知识资料]" in (m.get("content") or "")
                                   for m in msgs_w))

    # 同名笔记应该更新同一页，而不是每天新建
    learn_mod.NOTES = wiki_mod.NOTES
    learn_mod.note_write("装机清单", "补充：机箱选了 A700。")
    learn_mod.note_write("装机清单", "再补：电源换成 650W。")
    same = list(wiki_mod.NOTES.glob("*装机清单.md"))
    check("同名笔记只有一页", len(same) == 1, str([p.name for p in same]))
    check("补充内容写进同一页", "650W" in same[0].read_text(encoding="utf-8")
          if same else False)

    # ---------- AA. 主动消息写回历史 + 写库降级 ----------
    push_sid = "private:push"
    await core.record_assistant(push_sid, "我主动发的一条提醒")
    hist_push = await core.history(push_sid)
    check("主动消息进了历史",
          any("主动发的一条提醒" in h["content"] for h in hist_push),
          str(len(hist_push)))

    real_remember = core.remember

    async def boom_remember(*a, **kw):
        raise sqlite3.OperationalError("database is locked")

    core.remember = boom_remember
    try:
        bad = await core.remember_safe(push_sid, "user", "写不进去")
        # 现在返回行号（失败为 0），调用方按真假值判断
        check("写库失败返回假值而不是抛异常", not bad, str(bad))

        async def ok_raw(msgs, tools_arg=None, **kw):
            return {"content": "还行。", "tool_calls": []}

        core.complete_raw = ok_raw
        rp, _ = await core.reply("private:dbdown", "在吗")
        check("写库挂了也照样回复", bool(rp), str(rp))
    finally:
        core.remember = real_remember

    mems_age = await core.memory.recall("测试", 3)
    check("召回结果带年龄字段", all("age_days" in m for m in mems_age) if mems_age else True,
          str(mems_age[:1])[:60])
    full = await core.memory.read_full("测试")
    check("memory_read 有返回", bool(full), full[:30])

    # ---------- AC. LLM 客户端：错误分类 / 重试 / 用途埋点 ----------
    check("400 不重试", not chat_mod._retryable(
        httpx.HTTPStatusError("bad", request=httpx.Request("POST", "http://x"),
                               response=httpx.Response(400))))
    check("402 余额不足不重试", not chat_mod._retryable(
        httpx.HTTPStatusError("pay", request=httpx.Request("POST", "http://x"),
                               response=httpx.Response(402))))
    check("503 可重试", chat_mod._retryable(
        httpx.HTTPStatusError("busy", request=httpx.Request("POST", "http://x"),
                               response=httpx.Response(503))))
    check("连接错误可重试", chat_mod._retryable(httpx.ConnectError("boom")))

    # 402 只该发一次，不能重试三次
    hits = {"n": 0}

    def pay_handler(request):
        hits["n"] += 1
        return httpx.Response(402, json={"error": {"message": "Insufficient Balance"}})

    def pay_client(*a, **kw):
        kw["transport"] = httpx.MockTransport(pay_handler)
        return real_client(*a, **kw)

    core.__dict__.pop("complete_raw", None)
    with patch("httpx.AsyncClient", pay_client):
        try:
            await core.complete_raw([{"role": "user", "content": "x"}])
            paid_err = None
        except Exception as e:  # noqa: BLE001
            paid_err = e
    check("402 只请求一次就放弃", hits["n"] == 1, str(hits["n"]))
    check("402 会把异常抛出来", paid_err is not None, type(paid_err).__name__)

    # 总结：两个模型名相同时只跑一个
    core.__dict__.pop("complete_summary", None)   # 前面 wiki 用例换过实例属性
    core.summary_model = "deepseek-chat"
    core.summary_retry = "deepseek-chat"
    summary_calls = {"n": 0}

    async def boom_complete(*a, **kw):
        summary_calls["n"] += 1
        raise RuntimeError("down")

    core.complete_raw = boom_complete
    got = await core.complete_summary([{"role": "user", "content": "x"}])
    check("同模型不再重复跑两圈", summary_calls["n"] <= 2, str(summary_calls["n"]))
    check("总结失败返回空串", got == "", repr(got))

    # 用途埋点
    con = sqlite3.connect(TMP / "chat.db")
    cols = [r[1] for r in con.execute("PRAGMA table_info(usage)")]
    con.close()
    check("usage 有 purpose 列", "purpose" in cols, str(cols))

    # 流式首字前失败 → 回退非流式
    core.__dict__.pop("complete_raw", None)
    calls2 = {"stream": 0}

    async def bad_stream(*a, **kw):
        calls2["stream"] += 1
        raise httpx.ConnectError("stream down")

    async def ok_raw(msgs, tools_arg=None, **kw):
        return {"content": "回退成功。", "tool_calls": []}

    core._stream_raw = bad_stream
    core.complete_raw = ok_raw

    async def sink(_s):
        pass

    rp3, _ = await core.reply("private:fallback", "在吗", on_sentence=sink)
    check("流式挂了会回退非流式", "回退成功" in "".join(rp3 or []), str(rp3))

    # 流式：结尾建议先扣住，最后丢弃；正文照常发出
    acalls = {"n": 0}

    async def advice_stream(msgs, tools_arg=None, cb=None, **kw):
        acalls["n"] += 1
        # 节奏照真实流式：句子要等下一个字到了才冲刷；结尾建议先扣住，
        # 最后一个增量只剩空白 -> 扣住的那句判定为结尾，丢弃。
        for d in ("好的。x", " 建议你早点睡。", " "):
            await cb(d)
        return {"content": "好的。x 建议你早点睡。", "tool_calls": []}

    core._stream_raw = advice_stream
    got: list[str] = []

    async def collect(s):
        got.append(s)

    await core.reply("private:advice", "嗯", on_sentence=collect)
    check("流式：结尾建议被扣住没发出去", not any("建议" in x for x in got), str(got))
    check("流式：正文照常发出", any("好的" in x for x in got),
          f"got={got} calls={acalls['n']}")

    # 未完成的工具轮候选正文不投递；断流转完整回复。
    async def cut_stream(msgs, tools_arg=None, cb=None, **kw):
        await cb("第一句。")
        raise httpx.ConnectError("断流")

    core._stream_raw = cut_stream
    got2: list[str] = []

    async def collect2(s):
        got2.append(s)

    after_cut, _ = await core.reply("private:cut", "在吗", on_sentence=collect2)
    con2 = sqlite3.connect(TMP / "chat.db")
    last = con2.execute(
        "SELECT content FROM messages WHERE session='private:cut' AND role='assistant' "
        "ORDER BY id DESC LIMIT 1").fetchone()
    con2.close()
    check("断流：候选片段未发送，回退正文进入历史",
          not got2 and "回退成功" in "".join(after_cut) and bool(last) and "回退成功" in last[0],
          (last[0] if last else "none")[:40])

    core.__dict__.pop("_stream_raw", None)
    core.__dict__.pop("complete_raw", None)

    # ---------- AB. 主动消息判断层（纯函数，可离线测） ----------
    from app_core import proactive as decide
    check("时段：22 点是晚上", decide.period(22) == "晚上", decide.period(22))
    from app_core import prompt as prompt_mod
    check("now_line 带时段",
          "深夜" in prompt_mod.now_line(datetime(2026, 11, 14, 1, 28)))
    check("now_line 带间隔",
          "8 小时" in prompt_mod.now_line(datetime(2026, 11, 14, 1, 28), gap_hours=8))
    check("间隔短不写", "小时" not in prompt_mod.now_line(
        datetime(2026, 11, 14, 1, 28), gap_hours=2))
    from app_core import reality as reality_mod
    check("夜里说早安被记",
          "morning_word_at_night" in reality_mod.check("早上好", 23, "记忆"))
    check("没有来源的现势断言被记",
          "live_claim_no_source" in reality_mod.check("现在多少钱", 15, "无"))
    check("正常回复不记", reality_mod.check("嗯，那省事。", 15, "记忆") == [])
    from app_core import perception as perception_mod
    check("天气词表命中", perception_mod.should_read("今天下雨吗"))
    check("天气词表不误伤", not perception_mod.should_read("今天好累"))
    _pcalls = {"n": 0}

    async def _fake_wttr(name):
        _pcalls["n"] += 1
        return {"text": "晴 20°C", "as_of": 0, "source": "test"}

    _old_wttr = perception_mod._wttr
    perception_mod._wttr = _fake_wttr
    perception_mod.clear_cache()
    try:
        _a = await perception_mod.read("深圳")
        _b = await perception_mod.read("深圳")
    finally:
        perception_mod._wttr = _old_wttr
        perception_mod.clear_cache()
    check("天气缓存命中", _pcalls["n"] == 1 and bool(_a) and bool(_b), str(_pcalls))
    perception_mod.DB = TMP / "chat.db"
    _pc = sqlite3.connect(TMP / "chat.db")
    with _pc:
        _pc.execute("INSERT INTO messages(session, role, content, ts) VALUES(?,?,?,?)",
                    ("private:wx", "user", "明天出门要带伞吗", time.time()))
    _pc.close()
    check("天气：今天提过出门", perception_mod.mentioned_today("private:wx"))
    check("天气：没提过就不算", not perception_mod.mentioned_today("private:none"))
    check("天气突变：白天允许",
          perception_mod.alert_allowed(datetime(2026, 11, 14, 10)))
    check("天气突变：夜里不允许",
          not perception_mod.alert_allowed(datetime(2026, 11, 14, 2)))
    from app_core import correction as correction_mod
    check("纠正信号", correction_mod.looks_like_correction("不是这样，你记错了"))
    check("普通讨论不算纠正", not correction_mod.looks_like_correction("今天天气不错"))
    check("纠正哈希稳定",
          correction_mod._hash("他生日是3月") == correction_mod._hash(" 他生日是3月 "))

    async def _fake_fact(msgs, tools_arg=None, **kw):
        return {"content": json.dumps({"is_correction": True, "kind": "fact",
                                       "claim": "他生日是3月", "evidence": "不是2月，是3月", "confidence": 0.9})}

    _old_raw = core.complete_raw
    core.complete_raw = _fake_fact
    try:
        _cr = await correction_mod.adjudicate(core, "不是2月，是3月", "你生日是2月吧")
    finally:
        core.complete_raw = _old_raw
    check("纠正事实写进记忆", _cr == "fact", _cr)

    async def _fake_beh(msgs, tools_arg=None, **kw):
        return {"content": json.dumps({"is_correction": True, "kind": "behavior",
                                       "claim": "[文字] 别只顺着说", "evidence": "你别只顺着我说", "confidence": 0.9})}

    core.complete_raw = _fake_beh
    try:
        _cb = await correction_mod.adjudicate(core, "你别只顺着我说", "嗯")
    finally:
        core.complete_raw = _old_raw
    check("纠正行为写进清单", _cb == "behavior", _cb)

    async def _fake_low(msgs, tools_arg=None, **kw):
        return {"content": json.dumps({"is_correction": True, "kind": "fact",
                                       "claim": "", "confidence": 0.3})}

    core.complete_raw = _fake_low
    try:
        _cp = await correction_mod.adjudicate(core, "我都说了我在深圳", "你在广州吧")
    finally:
        core.complete_raw = _old_raw
    check("提不出的事实进 pending", _cp == "pending", _cp)
    _corr_txt = (learn_store_mod.CORRECTIONS.read_text(encoding="utf-8")
                 if learn_store_mod.CORRECTIONS.exists() else "")
    check("pending 不进行为清单", "我在深圳" not in _corr_txt)
    async def _fake_fact2(msgs, tools_arg=None, **kw):
        return {"content": json.dumps({"is_correction": True, "kind": "fact",
                                       "claim": "他生日是3月", "evidence": "不是2月，是3月", "confidence": 0.9})}

    core.complete_raw = _fake_fact2
    try:
        await correction_mod.adjudicate(core, "不是2月，是3月", "你生日是2月吧")
        await correction_mod.adjudicate(core, "不是2月，是3月", "你生日是2月吧")
    finally:
        core.complete_raw = _old_raw
    _mc = sqlite3.connect(TMP / "livingmemory.db")
    # 按**精确文本**数，不按 LIKE '%生日%'：这份库是生产库的只读副本，
    # 生产里只要有一条提到「生日」的记忆，LIKE 就会数成 2、把去重逻辑判成坏的
    # （2026-11-22 实测：线上新增一条含「生日」的记忆后这里误红）。
    _n3 = _mc.execute("select count(*) from documents where text=?",
                      ("他生日是3月",)).fetchone()[0]
    _mc.close()
    check("同一条纠正只写一次", _n3 == 1, str(_n3))

    # 复盘：pending 重试一次就分流
    correction_mod.PENDING.write_text("", encoding="utf-8")
    core.complete_raw = _fake_low
    try:
        await correction_mod.adjudicate(core, "我都说了我在深圳", "你在广州吧")
    finally:
        core.complete_raw = _old_raw

    async def _fake_pending_ok(msgs, tools_arg=None, **kw):
        return {"content": json.dumps({"is_correction": True, "kind": "fact",
                                       "claim": "他在深圳", "evidence": "我都说了我在深圳", "confidence": 0.9})}

    core.complete_raw = _fake_pending_ok
    try:
        _done = await correction_mod.process_pending(core, limit=5)
    finally:
        core.complete_raw = _old_raw
    check("复盘点处理 pending", _done >= 1, str(_done))
    check("pending 处理后被移除", correction_mod._read(correction_mod.PENDING) == [])
    check("时段：7 点是早上", decide.period(7) == "早上", decide.period(7))
    check("时段：2 点是深夜", decide.period(2) == "深夜", decide.period(2))
    check("时段：14 点是下午", decide.period(14) == "下午", decide.period(14))
    ok, why = decide.gate(3, 99999, 99999, 0, 4, {}, [0.1, 0.1, 0.1])
    check("夜里不开口", not ok and "时段" in why, why)
    ok, why = decide.gate(15, 60, 99999, 0, 4, {}, [0.1, 0.1, 0.1])
    check("刚发过不开口", not ok and "太近" in why, why)
    ok, why = decide.gate(15, 99999, 60, 0, 4, {}, [0.1, 0.1, 0.1])
    check("刚聊过不打扰", not ok and "刚聊" in why, why)
    ok, why = decide.gate(15, 99999, 99999, 4, 4, {}, [0.1, 0.1, 0.1])
    check("今日已满不开口", not ok and "已满" in why, why)
    # 2026-11-10 收窄：没有话头就不开口，不再抽签（11 天数据：泛话题 4 条 0 回复）
    ok, why = decide.gate(15, 99999, 99999, 0, 4, {}, [0.1, 0.1, 0.1])
    check("没有话头不没话找话", not ok and "话头" in why, why)
    ok, why = decide.gate(15, 99999, 99999, 0, 4,
                          {"valence": -0.8, "energy": 0.2}, [0.1, 0.1, 0.1], threads=1)
    check("心情差不拦着开口（只影响语气）", ok and "话头" in why, why)
    ok, why = decide.gate(15, 99999, 600, 0, 4, {}, [0.1, 0.1, 0.1], threads=2)
    check("刚聊过 10 分钟就别凑上去", not ok and "刚聊" in why, why)
    ok, why = decide.gate(15, 99999, 8 * 3600, 0, 4, {}, [0.1, 0.1, 0.1], threads=2)
    check("他留的话头还悬着就该开口", ok and "话头" in why, why)
    ok, why = decide.gate(15, 99999, 8 * 3600, 0, 4, {}, [0.1, 0.1, 0.1], threads=0)
    check("没话头就不开口", not ok and "话头" in why, why)

    check("回应率 0 只给 1 条", decide.daily_cap(0.0) == 1, str(decide.daily_cap(0.0)))
    check("回应率 30% 克制为 1 条", decide.daily_cap(0.3) == 1)
    check("高回应率给 2 条", decide.daily_cap(0.6) == 2)
    check("没有样本保守给 1 条", decide.daily_cap(None) == 1)

    _fu = [("fu:1", "交周报", "问进展")]
    _cu = [("cur:1", "没搞懂的事", "问一句")]
    _me = [("mem:1", "很久没提的事", "想起来")]
    check("优先问没做完的事",
          decide.pick_topic(_fu, _cu, _me, set(), 0.1, 0.5)[0] == "fu:1")
    check("跳过一周内提过的",
          decide.pick_topic([], _cu, _me, {"cur:1"}, 0.6, 0.0)[0] == "mem:1")
    check("都没话题就不开口", decide.pick_topic([], [], [], set(), 0.1, 0.5) is None)

    # ---------- Z2. 隐形字符清理 + 索引分组 ----------
    _zw = chr(0x200B)
    _wj = chr(0x2060)
    _tag = chr(0xE0041) + chr(0xE0042)
    dirty = "正常内容" + _zw + "带零宽" + _wj + "和标签" + _tag
    clean = files_mod.sanitize(dirty)
    check("剥掉零宽字符", _zw not in clean and _wj not in clean, repr(clean))
    check("剥掉 unicode tag block",
          all(not (0xE0000 <= ord(c) <= 0xE007F) for c in clean))
    check("正文不被误删", "正常内容" in clean and "带零宽" in clean, clean)

    p_dirty = TMP / "dirty.txt"
    p_dirty.write_text(dirty, encoding="utf-8")
    t_clean, _ = files_mod.parse(p_dirty)
    check("parse 出口已消毒", _zw not in t_clean, repr(t_clean[:20]))

    con = sqlite3.connect(TMP / "chat.db")
    with con:
        con.execute("INSERT OR REPLACE INTO wiki_pages"
                    "(path, title, summary, project, mtime, ts) VALUES (?,?,?,?,?,?)",
                    ("/x/a.md", "页A", "摘要A", "项目甲", time.time(), time.time()))
        con.execute("INSERT OR REPLACE INTO wiki_pages"
                    "(path, title, summary, project, mtime, ts) VALUES (?,?,?,?,?,?)",
                    ("/x/b.md", "页B", "摘要B", "项目乙", time.time(), time.time()))
    con.close()
    txt2 = wiki_mod.index_text()
    check("索引按项目分组", "项目甲：" in txt2 and "项目乙：" in txt2, txt2[:40])

    # ---------- Y. 待办过期：只增不减会烂在库里 ----------
    for i in range(3):
        followup_mod.add("老待办%d" % i, "memory", "doc-old-%d" % i)
    con = sqlite3.connect(TMP / "chat.db")
    with con:
        con.execute("UPDATE followups SET created_ts=? WHERE text LIKE '老待办%'",
                    (time.time() - 70 * 86400,))
    con.close()
    r_exp = followup_mod.expire()
    check("超过 60 天的待办被取消", r_exp["old"] >= 3, str(r_exp))

    followup_mod.add("新待办不该被误杀", "memory", "doc-new")
    con = sqlite3.connect(TMP / "chat.db")
    with con:
        con.execute("UPDATE followups SET ask_count=9 WHERE text=?", ("新待办不该被误杀",))
    con.close()
    followup_mod.expire()
    left = {x["text"] for x in followup_mod.pending(500)}
    check("问过太多次的也被取消", "新待办不该被误杀" not in left)
    check("未关闭数量被压到上限内", len(left) <= followup_mod.KEEP_MAX_OPEN + 1,
          str(len(left)))

    return patch, wiki_mod
