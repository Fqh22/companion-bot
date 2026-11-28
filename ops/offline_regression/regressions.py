"""离线回归组，跨组状态由入口显式传递。"""
from offline_regression.fixtures import (
    asyncio, os, sqlite3, time, TMP, chat_mod, config_mod, files_mod, followup_mod, kb_mod,
    learn_mod, memory_mod, mood_mod, followup_store_mod, remind_mod, summarize_mod,
    tools_module, REAL_TOOLS_EXECUTE, check,
)
from offline_regression import adapters, pacing


async def run(_P, core, fresh_mood, learn_store_mod, patch, prompt_fresh, wiki_mod):
    # ---------- Z9. 识图：分辨率 / EXIF / 提示 / 预算 ----------
    from io import BytesIO

    from PIL import Image as _Img
    from PIL import ImageDraw as _Draw

    def _make_shot(w=1080, h=2400) -> bytes:
        im = _Img.new("RGB", (w, h), (250, 250, 250))
        d = _Draw.Draw(im)
        d.rectangle([0, 0, w, 90], fill=(40, 40, 40))
        for i in range(30):
            d.rectangle([40, 140 + i * 60, w - 120, 160 + i * 60], fill=(60, 60, 60))
        buf = BytesIO()
        im.save(buf, format="PNG")
        return buf.getvalue()

    out_shot = files_mod._shrink(_make_shot())
    sz = _Img.open(BytesIO(out_shot)).size
    check("截图按 1600 长边压", max(sz) == 1600, str(sz))

    # 配置能把上限调回去（嫌慢时用）
    saved_cache2 = config_mod._cache
    try:
        config_mod._cache = {**config_mod.load(), "image": {"max_side": 1024, "quality": 85}}
        out_small = files_mod._shrink(_make_shot())
        sz2 = _Img.open(BytesIO(out_small)).size
        check("image.max_side 可配置", max(sz2) == 1024, str(sz2))
    finally:
        config_mod._cache = saved_cache2

    # EXIF 方向：竖着存的横图，读完应该是横的
    rot = _Img.new("RGB", (400, 200), (200, 30, 30))
    ex = _Img.Exif()
    ex[0x0112] = 6                      # 需要顺时针转 90° 才正
    buf_rot = BytesIO()
    rot.save(buf_rot, format="JPEG", exif=ex)
    out_rot = files_mod._shrink(buf_rot.getvalue())
    sz_rot = _Img.open(BytesIO(out_rot)).size
    check("EXIF 旋转被纠正", sz_rot[0] < sz_rot[1], str(sz_rot))

    # 带图的 prompt 要有转录提示
    msgs_img = core.build_messages([], "这报错怎么改", images=[str(TMP / "nope.jpg")])
    text_part = msgs_img[-1]["content"][0]["text"] if isinstance(
        msgs_img[-1]["content"], list) else ""
    check("带图 prompt 有转录提示", "逐字转录" in text_part, text_part[-30:])
    # 求助图才走「图文比对 + 细读」；分享图只要接住。
    check("图片提示要求比对图文", "图在说什么" in text_part and "矛盾" in text_part)
    check("排错场景仍要求逐字转录", "逐字转录关键行" in text_part)
    check("矛盾时不拆穿反讽", "别拆穿反讽" in text_part)
    check("判错时按他的纠正来", "按他下一句纠正" in text_part)
    check("求助图拿不准先搜", "拿不准先搜" in text_part)
    check("求助图多图按组", "多图按组" in text_part and "别逐张点评" in text_part)

    msgs_share = core.build_messages([], "哈哈", images=[str(TMP / "nope.jpg")])
    share_text = msgs_share[-1]["content"][0]["text"] if isinstance(
        msgs_share[-1]["content"], list) else ""
    check("分享图不按题目分析", "不是题目" in share_text and "别分析" in share_text,
          share_text[-40:])
    check("分享图接住氛围", "接住" in share_text, share_text[-40:])
    check("分享图不拆文字", "别分析构图或文字" in share_text, share_text[-40:])

    # 有图的轮次要把预算提上去（思考会吃输出）
    seen_mt = {}

    async def fake_complete(msgs, tools_arg=None, max_tokens=None, **kw):
        seen_mt["mt"] = max_tokens
        return {"content": "x", "tool_calls": []}

    async def fake_run(_core, _msgs, *, complete, stream, **_kw):
        await complete([], None)
        return "ok"

    real_run2, real_complete = chat_mod.tool_loop.run, core.complete_raw
    chat_mod.tool_loop.run = fake_run
    core.complete_raw = fake_complete
    try:
        await core._run_tools([], images=["/x.jpg"], budget=chat_mod.IMAGE_MAX_TOKENS)
        check("有图轮次带上更大输出预算",
              seen_mt.get("mt") == chat_mod.IMAGE_MAX_TOKENS, str(seen_mt))
    finally:
        chat_mod.tool_loop.run = real_run2
        core.complete_raw = real_complete

    # ---------- Z10. 形式约束：压掉「附和+解释+反问」的复读 ----------
    from app_core import prompt as prompt_mod2

    h_none = prompt_mod2.shape_hint([], {"energy": 0.7})
    check("没有历史时也给形式约束", "只做一件事" in h_none, h_none)

    hist_ack = [{"role": "assistant", "content": "对，这样可以"},
                {"role": "assistant", "content": "嗯，我想想"}]
    h1 = prompt_mod2.shape_hint(hist_ack, {"energy": 0.7})
    check("连续附和词开头会被约束", "别用「对/嗯/哦/行」开头" in h1, h1)

    hist_q = [{"role": "assistant", "content": "真的？"},
              {"role": "assistant", "content": "你确定？"}]
    h2 = prompt_mod2.shape_hint(hist_q, {"energy": 0.7})
    check("连续问句收尾会被约束", "别用问句收尾" in h2, h2)

    h3 = prompt_mod2.shape_hint(hist_ack, {"energy": 0.2})
    check("精力低时要求更短", ("说短点" in h3 or "没力气" in h3), h3)

    h4 = prompt_mod2.shape_hint(hist_ack, {"energy": 0.3, "social": 0.9})
    check("热度高时允许接话茬", "接他的话茬" in h4, h4)

    # 口头禅是频率问题：间隔出现的复读也要拦（旧判据要求连续 3 条完全相同）
    hist_habit = [{"role": "assistant", "content": t} for t in
                  ("那就先分清", "我也不确定", "那就先别急", "这个有点难",
                   "那就不劝了", "嗯明白", "那就更没法管了", "行吧", "我不知道", "随便")]
    h5 = prompt_mod2.shape_hint(hist_habit, {"energy": 0.7})
    check("间隔复读（那就×4）会被拦", "那就" in h5, h5)
    h6 = prompt_mod2.shape_hint([{"role": "assistant", "content": "好"}] * 2, {"energy": 0.7})
    check("样本太少不误报口头禅", "最近老用" not in h6, h6)

    # ---- 长度预算：只在会跑偏时注入，且不与 shape_hint 打架 ----
    lh = prompt_mod2.length_hint
    check("闲聊短句且回复不啰嗦：不干预",
          lh("在吗", {"energy": 0.7}, [20, 18, 22]) == "", lh("在吗", {"energy": 0.7}, [20, 18, 22]))
    check("他短我长：压短",
          "一两句" in lh("在", {"energy": 0.7}, [54, 61, 48]), lh("在", {"energy": 0.7}, [54, 61, 48]))
    check("他问得具体：允许展开",
          "可以展开" in lh("为什么推理模式会让首字变慢，这个在深层原理上是怎么冒出来的", {}),
          lh("为什么推理模式会让首字变慢，这个在深层原理上是怎么冒出来的", {}))
    check("随口一问不算技术咨询",
          "可以展开" not in lh("怎么了", {"energy": 0.7}, [20, 20, 20]),
          lh("怎么了", {"energy": 0.7}, [20, 20, 20]))
    # 弱词必须配问号：陈述句里出现「对比」不该被当成求解释（真实历史踩到过）
    check("陈述句里的「对比」不触发展开",
          "可以展开" not in lh("然后我的输入会与向量库的检索对比", {}),
          lh("然后我的输入会与向量库的检索对比", {}))
    check("问句里的「对比」可以展开",
          "可以展开" in lh("这两套记忆方案的区别对比一下？", {}),
          lh("这两套记忆方案的区别对比一下？", {}))
    check("他在说自己的事：别说教",
          "别说教" in lh("今天真的累死了", {"energy": 0.7}, [40, 40, 40]),
          lh("今天真的累死了", {"energy": 0.7}, [40, 40, 40]))
    check("情绪低落时收短",
          "短一点" in lh("哦", {"energy": 0.7, "valence": -0.4, "valence_base": 0.0}, [20, 20, 20]),
          lh("哦", {"energy": 0.7, "valence": -0.4, "valence_base": 0.0}, [20, 20, 20]))
    check("连发几条可以多接",
          "接一句长的" in lh("那个事", {"energy": 0.7}, [20, 20, 20], burst=3),
          lh("那个事", {"energy": 0.7}, [20, 20, 20], burst=3))
    # 自限：一旦回复变短，压短的条件自动失效（否则会变成新的固定长度）
    check("回复变短后压短指令自动消失",
          lh("在", {"energy": 0.7}, [18, 22, 20]) == "",
          lh("在", {"energy": 0.7}, [18, 22, 20]))

    # 两条形式指令不能打架：允许展开时不再说「说短点」
    hist_long = [{"role": "assistant", "content": "字" * 80}] * 3
    h_open = prompt_mod2.shape_hint(hist_long, {"energy": 0.7}, allow_short=False)
    h_keep = prompt_mod2.shape_hint(hist_long, {"energy": 0.7})
    check("允许展开时不再要求说短点",
          "说短点" not in h_open and "说短点" in h_keep, f"{h_open} / {h_keep}")

    # 长度改动不该反向影响精力模型：task_cost 只在 >120 字才按 120 字一档计费
    check("短回复之间的长度差不影响精力结算",
          mood_mod.task_cost(1, 30) == mood_mod.task_cost(1, 54),
          f"{mood_mod.task_cost(1, 30)} vs {mood_mod.task_cost(1, 54)}")

    # 时间锚点：模型必须知道今天几号，否则「明天九点」没法换算成绝对时间
    from datetime import datetime as _dt2
    nl = prompt_mod2.now_line(_dt2(2026, 11, 3, 12, 30))
    check("时间行含日期与星期", "2026-11-03" in nl and "周二" in nl and "12:30" in nl, nl)
    check("时间行说明用途", "明天" in nl, nl)
    check("周日映射正确", "周日" in prompt_mod2.now_line(_dt2(2026, 11, 8, 9, 0)),
          prompt_mod2.now_line(_dt2(2026, 11, 8, 9, 0)))

    # 索引预算必须和 prompt 侧的 clip(1000) 对齐
    from app_core import wiki as wiki_mod3
    check("索引上限与 prompt 预算对齐", 900 < wiki_mod3.INDEX_MAX_CHARS <= 1000,
          str(wiki_mod3.INDEX_MAX_CHARS))

    # 保质期优先用写笔记时模型标的，没标（-1）才回落到关键词；显式 0 = 永久
    check("模型标的保质期优先于关键词",
          wiki_mod3.page_ttl({"title": "显卡价格", "summary": "", "stale_days": 90}) == 90,
          str(wiki_mod3.page_ttl({"title": "显卡价格", "summary": "", "stale_days": 90})))
    check("没标时回落到关键词",
          wiki_mod3.page_ttl({"title": "显卡价格", "summary": "", "stale_days": -1}) == 14)
    check("显式「永久」不再回落关键词",
          wiki_mod3.page_ttl({"title": "显卡价格", "summary": "", "stale_days": 0}) > 365,
          str(wiki_mod3.page_ttl({"title": "显卡价格", "summary": "", "stale_days": 0})))
    check("文件里有保质期字段能解析",
          learn_mod.note_meta("# t\n\n- 保质期：30天\n")["stale_days"] == 30)
    check("「永久」解析成 0（显式有效）",
          learn_mod.note_meta("# t\n\n- 保质期：永久\n")["stale_days"] == 0)
    check("没有保质期字段解析成 -1（未标）",
          learn_mod.note_meta("# t\n\n- 来源：自撰\n")["stale_days"] == -1)

    # ---- 自动归档：只收「很久没人读 + 过期或未验证」的页 ----
    from app_core import wiki as wiki_arch
    now_a = time.time()
    day_s = 86400

    def _pg(title, idle_days, verified_days=None, stale_days=-1):
        return {"title": title, "summary": title, "status": "active",
                "mtime": now_a - idle_days * day_s, "last_open_ts": 0,
                "verified_ts": (now_a - verified_days * day_s) if verified_days else 0,
                "stale_days": stale_days}

    check("很久没人读且过期 → 归档候选",
          len(wiki_arch.archive_candidates([_pg("显卡价格", 120)], now=now_a)) == 1)
    check("最近读过就不归档",
          not wiki_arch.archive_candidates([_pg("显卡价格", 10)], now=now_a))
    check("验证过且在保质期内 → 不归档",
          not wiki_arch.archive_candidates(
              [_pg("他喜欢喝茶", 120, verified_days=5)], now=now_a))
    check("长期没人读且从未验证 → 归档候选",
          len(wiki_arch.archive_candidates([_pg("随手记", 120)], now=now_a)) == 1)
    _old = _pg("旧页", 200)
    _old["status"] = "已废弃"
    check("已废弃的不再重复处理",
          not wiki_arch.archive_candidates([_old], now=now_a))

    # ---- 归档动作本体：真的改文件头 + 改库（此前只测了候选筛选） ----
    _saved_wdb = wiki_arch.DB
    wiki_arch.DB = _P(TMP) / "wiki_arch.db"
    _arch_dir = _P(TMP) / "notes_arch"
    _arch_dir.mkdir(parents=True, exist_ok=True)
    try:
        wiki_arch.init()
        _seed_files = []
        _old_ts = time.time() - 200 * 86400
        for i in range(3):
            _f = _arch_dir / f"2026-01-0{i + 1}-旧页{i}.md"
            _f.write_text(f"# 旧页{i}\n\n- 日期：2026-01-01\n- 来源：自撰\n"
                          f"- 状态：active\n- 保质期：14天\n\n正文\n", encoding="utf-8")
            os.utime(_f, (_old_ts, _old_ts))
            _seed_files.append(_f)
        _c = wiki_arch._conn()
        with _c:
            for _f in _seed_files:
                _c.execute(
                    "INSERT OR REPLACE INTO wiki_pages(path,title,summary,project,"
                    "mtime,ts,source,verified_ts,status,stale_days) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (str(_f), _f.stem[:14], "摘要", "通用", _old_ts, time.time(),
                     "自撰", 0, "active", 14))
        _c.close()
        check("归档前 3 页都在库里", len(wiki_arch.pages()) == 3,
              str(len(wiki_arch.pages())))
        _done = wiki_arch.archive_stale(max_pages=2)
        check("归档只处理上限内的条目", len(_done) == 2, str(len(_done)))
        check("归档后库里状态变成已废弃",
              sum(1 for p in wiki_arch.pages() if p["status"] == "已废弃") == 2,
              str([p["status"] for p in wiki_arch.pages()]))
        check("归档改了文件头（不是只改库）",
              any("已废弃" in f.read_text(encoding="utf-8") for f in _seed_files),
              str([("已废弃" in f.read_text(encoding="utf-8")) for f in _seed_files]))

        # ---- 物理删除的正常路径：打包 → 校验 → 删文件 → 删库行 ----
        from app_core import purge as purge_p
        _saved_bd, _saved_pdb = purge_p.BACKUP_DIR, wiki_arch.DB
        purge_p.BACKUP_DIR = _P(TMP) / "bk_purge"
        purge_p.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        wiki_arch.DB = _P(TMP) / "wiki_purge.db"
        try:
            wiki_arch.init()
            _pfiles = []
            _very_old = time.time() - 400 * 86400
            for i in range(2):
                _f = _P(TMP) / f"purge{i}.md"
                _f.write_text("内容", encoding="utf-8")
                os.utime(_f, (_very_old, _very_old))
                _pfiles.append(_f)
            _c = wiki_arch._conn()
            with _c:
                for _f in _pfiles:
                    _c.execute(
                        "INSERT OR REPLACE INTO wiki_pages(path,title,summary,project,"
                        "mtime,ts,source,verified_ts,status,stale_days) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (str(_f), "旧页", "摘要", "通用", _very_old, time.time(),
                         "自撰", 0, "已废弃", 14))
            _c.close()
            _r = purge_p.purge_knowledge(days=180, max_files=5)
            check("物理删除走了正常路径", _r["deleted"] == 2 and bool(_r["tarball"]),
                  str(_r))
            check("源文件真的被删了", not any(f.exists() for f in _pfiles))
            check("库里对应的行也清了", len(wiki_arch.pages()) == 0,
                  str(len(wiki_arch.pages())))
            check("删之前打了包", _P(_r["tarball"]).exists(), _r["tarball"])
        finally:
            purge_p.BACKUP_DIR, wiki_arch.DB = _saved_bd, _saved_pdb

        # ---- 调度器的两个非 JOBS 任务：周巡检 / 月度清理 ----
        from types import SimpleNamespace as _SNS
        from app_core import scheduler as sched_p, watch as watch_p, purge as purge_q
        _sent = []

        async def _send(t):
            _sent.append(t)
            return True

        _s = sched_p.Scheduler(_SNS(complete_summary=lambda *a, **k: None), _send)
        _saved_ca = watch_p.check_all

        async def _fake_ca(search, complete):
            return ["显卡价格：涨了 200"]

        watch_p.check_all = _fake_ca
        try:
            await _s._run_watch()
            check("周巡检有变化会发消息", any("有变化" in x for x in _sent), str(_sent))
        finally:
            watch_p.check_all = _saved_ca

        _saved_pk = purge_q.purge_knowledge
        _saved_rot = purge_q.rotate
        _saved_pr = purge_q.prune_old_packs
        purge_q.purge_knowledge = lambda *a, **k: {"deleted": 2,
                                                   "tarball": "/tmp/x.tgz",
                                                   "titles": ["旧页一", "旧页二"]}
        purge_q.rotate = lambda *a, **k: []
        purge_q.prune_old_packs = lambda *a, **k: []
        _sent.clear()
        try:
            await _s._run_purge()
            check("月度清理有删除会发消息", any("本月清理" in x for x in _sent),
                  str(_sent))
        finally:
            purge_q.purge_knowledge = _saved_pk
            purge_q.rotate, purge_q.prune_old_packs = _saved_rot, _saved_pr
    finally:
        wiki_arch.DB = _saved_wdb

    # ---- 纠正回流：以前只写文件，聊天时看不见，等于没学到 ----
    _saved_corr = learn_store_mod.CORRECTIONS
    learn_store_mod.CORRECTIONS = _P(TMP) / "corrections.md"
    try:
        learn_mod.write_list(learn_store_mod.CORRECTIONS, "他纠正过我的",
                             ["[图片] 别瞎猜糊图", "[文字] 别一口气发太长",
                              "[通用] 别只顺着说", "没标签的老条目"])
        got = learn_mod.recent_corrections(6)
        check("纠正条目能读出来", len(got) == 4, str(got))
        _m1 = prompt_fresh.build_messages({}, [], "在吗", lessons="\n".join(got))
        check("纠正确实进了 prompt",
              any("他纠正过你的事" in (x.get("content") or "") for x in _m1))
        _m2 = prompt_fresh.build_messages({}, [], "在吗")
        check("没有纠正时不注入空块",
              not any("他纠正过你的事" in (x.get("content") or "") for x in _m2))
        check("纠正条数可以限制", len(learn_mod.recent_corrections(1)) == 1)

        # 按场景分流：图片类是当前轮看图才需要，别挤进纯文字轮
        img_c = learn_mod.recent_corrections(9, scene="image")
        txt_c = learn_mod.recent_corrections(9, scene="text")
        check("图片轮带图片类+通用类", len(img_c) == 3 and "[图片]" in " ".join(img_c),
              str(img_c))
        check("图片轮不带文字类", not any("[文字]" in x for x in img_c), str(img_c))
        check("文字轮带文字类+通用类", len(txt_c) == 3 and "[文字]" in " ".join(txt_c),
              str(txt_c))
        check("文字轮不带图片类", not any("[图片]" in x for x in txt_c), str(txt_c))
        check("没标签的老条目按通用处理，两类都带",
              any("没标签" in x for x in img_c) and any("没标签" in x for x in txt_c))

        # ---- 写入端：复盘写进来的纠正必须带标签，否则分流会退化 ----
        _saved_w = (learn_store_mod.LEARN, learn_store_mod.CORRECTIONS,
                    learn_store_mod.CURIOUS, learn_store_mod.PROFILE)
        learn_store_mod.LEARN = _P(TMP) / "learn_w"
        learn_store_mod.LEARN.mkdir(parents=True, exist_ok=True)
        learn_store_mod.CORRECTIONS = learn_store_mod.LEARN / "corrections.md"
        learn_store_mod.CURIOUS = learn_store_mod.LEARN / "curious.md"
        learn_store_mod.PROFILE = learn_store_mod.LEARN / "self_profile.md"
        try:
            res_w = learn_mod.apply_review({
                "learned": "当…时，做…", "profile": "我是他手机里的熟人",
                "corrections": ["[图片] 别瞎猜糊图", "别一口气发太长", "[文字] 别催我"],
                "curious": ["还没搞懂 X"], "resolved_curious": []},
                day="2026-11-03")
            raw_corr = learn_store_mod.CORRECTIONS.read_text(encoding="utf-8")
            check("复盘写入的纠正保留原标签", "[图片] 别瞎猜糊图" in raw_corr, raw_corr[:40])
            check("没标签的纠正被补成通用", "[通用] 别一口气发太长" in raw_corr, raw_corr[-30:])
            check("apply_review 返回纠正条数", res_w["corrections"] == 3, str(res_w))
            check("信息缺口不进好奇清单",
                  not learn_mod.curious_ok("他的家庭情况还没提过"))
            check("真正的技术疑问保留",
                  learn_mod.curious_ok("跨库 join 和 livingmemory 会话语义搜索怎么解决的"))
            check("复盘日记被写出", (learn_store_mod.LEARN / "2026-11-03.md").exists())
            check("自我认知被更新",
                  "熟人" in learn_store_mod.PROFILE.read_text(encoding="utf-8"))
            _img_after = learn_mod.recent_corrections(9, scene="image")
            _txt_after = learn_mod.recent_corrections(9, scene="text")
            check("写进去之后图片轮只带图片+通用",
                  any("别瞎猜糊图" in x for x in _img_after)
                  and any("别一口气" in x for x in _img_after)
                  and not any("别催我" in x for x in _img_after), str(_img_after))
            check("写进去之后文字轮只带文字+通用",
                  any("别催我" in x for x in _txt_after)
                  and not any("别瞎猜糊图" in x for x in _txt_after), str(_txt_after))
            _tops = ["价格", "版本", "记忆", "情绪", "图", "文件", "部署", "日志",
                     "提醒", "日历", "语音", "表情", "知识", "检索", "总结", "复盘",
                     "群聊", "私聊", "命令", "权限", "备份", "清理", "测试", "索引",
                     "缓存", "预算", "超时", "重试", "并发", "冷启动"]
            _seed = [f"[通用] 遇到{t}相关的问题时先看日志再动手，编号{i}"
                     for i, t in enumerate(_tops)]
            learn_mod.write_list(learn_store_mod.CORRECTIONS, "他纠正过我的", _seed)
            _kept = learn_mod.merge_list(
                learn_store_mod.CORRECTIONS, "他纠正过我的", ["[通用] 全新的一条完全不同的规则"])
            check("合并去重后不超过上限，且保留最新那条",
                  len(_kept) <= 24 and _kept[-1].endswith("不同的规则"), str(len(_kept)))

            # ---- 相似度去重：换句话说的重复要并掉，不同的事不能动 ----
            check("近似重复判为重复", learn_mod.too_similar(
                "他讨厌「看情况」这类空话，要具体数字和区间。",
                "他讨厌我答「看情况」，要具体。"))
            check("不同的事不合并", not learn_mod.too_similar(
                "他明说一次发太多会截断", "他讨厌我答看情况"))
            check("标签不同但内容相同算重复",
                  learn_mod.too_similar("[图片] 别瞎猜", "[通用] 别瞎猜"))
            # 短条目不能模糊匹配：「问题一/问题二」相似度够高，但它们是两件事
            check("短条目只认精确匹配",
                  not learn_mod.too_similar("问题一", "问题二")
                  and learn_mod.too_similar("问题一", "问题一"))
            _lp = _P(TMP) / "merge_test.md"
            learn_mod.write_list(_lp, "t", ["他讨厌看情况这类空话要具体", "完全不同的一件事"])
            learn_mod.merge_list(_lp, "t", ["他讨厌我答看情况，要具体。"])
            _after_merge = learn_mod.read_list(_lp)
            check("写入时并掉近似重复", len(_after_merge) == 2, str(_after_merge))
        finally:
            (learn_store_mod.LEARN, learn_store_mod.CORRECTIONS,
             learn_store_mod.CURIOUS, learn_store_mod.PROFILE) = _saved_w
    finally:
        learn_store_mod.CORRECTIONS = _saved_corr

    # ---- 图片图注：追加到原消息，下一轮就能看到 ----
    _rid = await core.remember("private:imgtest", "user", "[发了图片]（附了 1 张图）")
    check("remember 返回行号", _rid > 0, str(_rid))
    _ok = await core.append_to_message(_rid, "｜图注：猫趴在键盘上")
    _con = sqlite3.connect(TMP / "chat.db")
    _row = _con.execute("SELECT content FROM messages WHERE id=?", (_rid,)).fetchone()
    _con.close()
    check("图注能追加到原消息",
          _ok and _row and "图注：猫趴在键盘上" in _row[0], str(_row))
    _ok2 = await core.append_to_message(_rid, "｜图注：猫趴在键盘上")
    _con = sqlite3.connect(TMP / "chat.db")
    _txt = _con.execute("SELECT content FROM messages WHERE id=?", (_rid,)).fetchone()[0]
    _con.close()
    check("重复图注不会追加两次",
          (not _ok2) and _txt.count("图注：") == 1, _txt)

    # 图注生成本身（只验接线：图进得去、话回得来，不验质量）
    import app_core.files as files_probe
    _saved_url, _saved_raw = files_probe.to_data_url, core.complete_raw
    files_probe.to_data_url = lambda p: "data:image/jpeg;base64,AAAA"

    async def _cap_raw(msgs, tools_arg=None, **kw):
        return {"content": "一只猫趴在键盘上"}

    core.complete_raw = _cap_raw
    try:
        _cap = await core.caption_images(["/tmp/x.jpg"])
        check("图注生成走通", "猫" in _cap, _cap)
        check("没有图片时返回空串", await core.caption_images([]) == "")
    finally:
        core.complete_raw = _saved_raw
        files_probe.to_data_url = _saved_url

    # ---- 物理删除：只清知识库，且三条件都满足才删 ----
    from app_core import purge as purge_mod

    def _dead(idle_days, verified=False, opened=False, status="已废弃"):
        return {"title": "废弃页", "summary": "", "status": status,
                "mtime": now_a - idle_days * day_s,
                "last_open_ts": (now_a - day_s) if opened else 0,
                "verified_ts": (now_a - day_s) if verified else 0,
                "stale_days": -1}

    check("废弃满 180 天且没人碰过 → 可物理删除",
          len(purge_mod.select_candidates([_dead(200)], now=now_a)) == 1)
    check("废弃不满 180 天 → 先留着",
          not purge_mod.select_candidates([_dead(100)], now=now_a))
    check("验证过的废弃页永久保留",
          not purge_mod.select_candidates([_dead(400, verified=True)], now=now_a))
    check("被打开过的废弃页不删",
          not purge_mod.select_candidates([_dead(400, opened=True)], now=now_a))
    check("活跃页不会被物理删除",
          not purge_mod.select_candidates([_dead(400, status="active")], now=now_a))

    # 备份轮转：每类只留最近 N 份
    _saved_bd = purge_mod.BACKUP_DIR
    purge_mod.BACKUP_DIR = _P(TMP) / "backups"
    try:
        purge_mod.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        for i in range(5):
            f = purge_mod.BACKUP_DIR / f"code-2026{i}.tgz"
            f.write_bytes(b"x")
            import os as _os
            _os.utime(f, (now_a - (5 - i) * 3600,) * 2)
        gone = purge_mod.rotate(("code-",), keep=2)
        left = sorted(p.name for p in purge_mod.BACKUP_DIR.glob("code-*.tgz"))
        check("备份轮转只留最近 N 份", len(left) == 2 and len(gone) == 3,
              f"{left} / 删 {len(gone)}")
    finally:
        purge_mod.BACKUP_DIR = _saved_bd

    # 抓网页复用同一个客户端：每次新建都要多一次 TLS 握手
    from app_core import web as web_mod
    check("web 抓取复用同一个客户端", web_mod._http() is web_mod._http())

    # 注入位置：必须在历史之后，不能破坏稳定前缀（缓存）
    msgs_h = core.build_messages([{"role": "user", "content": "嗯"}], "在吗",
                                 extra_system=["【这次的形式】测试"])
    idx_hist = next(i for i, x in enumerate(msgs_h) if x.get("role") == "user")
    idx_hint = next(i for i, x in enumerate(msgs_h)
                    if "【这次的形式】" in (x.get("content") or ""))
    check("形式约束排在历史之后", idx_hint > idx_hist, f"{idx_hist} -> {idx_hint}")

    # 多块顺序：extra_system 是 list，拼接必须保持顺序。
    # 之前只有「单块在历史之后」这一条——多个注入块之间谁先谁后没有任何断言，
    # 并发化或重排时顺序漂移不会被发现。这里把契约补上。
    msgs_o = core.build_messages([], "在吗",
                                 extra_system=["【块一】甲", "【块二】乙", "【块三】丙"])
    _pos = {}
    for _k in ("块一", "块二", "块三"):
        _pos[_k] = next(i for i, x in enumerate(msgs_o)
                        if ("【%s】" % _k) in (x.get("content") or ""))
    check("多个注入块的顺序与传入一致",
          _pos["块一"] < _pos["块二"] < _pos["块三"], str(_pos))

    # 分享轮档位（docs/04-评测与实验.md）：判「他给了多少授权」，给一个读深度的上限
    from app_core import share_tier as st_mod
    check("无字 + 情绪物 → 第 0 档", st_mod.judge("", "Q版卡通人物鼓脸生气")[0] == 0)
    check("无字 + 作品 → 第 1 档", st_mod.judge("", "深色背景上的架构列表")[0] == 1)
    check("有字无指向 → 第 2 档", st_mod.judge("今天拍的这个", "")[0] == 2)
    check("明确要求 → 第 3 档", st_mod.judge("你觉得这个设计怎么样", "")[0] == 3)
    check("他说出意图 → 第 4 档", st_mod.judge("我这个方案想解决噪音", "")[0] == 4)
    check("每档都有下限（必须指认）",
          all("必须" in st_mod.block(t) for t in (0, 1, 2)))
    check("档位块带理由", "无字" in st_mod.block(*st_mod.judge("", "表情包")))

    # 姿态（docs/04-评测与实验.md §3）：用他的动作回应他，别当评论员
    from app_core import prompt as _p_mod
    check("自检规则含「接梗 / 看」两类",
          "接梗" in _p_mod.SELF_CHECK_RULE and "看（指认一个点" in _p_mod.SELF_CHECK_RULE)
    check("「玩笑」只归接梗，不再同时归轻接",
          _p_mod.SELF_CHECK_RULE.count("玩笑") == 1)
    check("7 个主类，封顶不再加",
          "接住|判断|事实|执行|轻接|接梗|看" in _p_mod.SELF_CHECK_RULE)
    check("雷区含「评写法」", "评写法" in _p_mod.LANDMINES)
    check("尾锚含「别当评论员」", "评论员" in _p_mod.MODEL_FIRST_TAIL)

    # ---------- Z11. 跨层：写完就能看见 / 删了要清 / 名字要对得上 ----------
    learn_mod.NOTES = wiki_mod.NOTES
    learn_mod.SKILLS = wiki_mod.SKILLS
    learn_mod.note_write("跨层探针页", "正文：R75800H 没有独显。", tag="测试")
    idx_now = wiki_mod.index_text()
    check("笔记写完立刻进索引", "跨层探针页" in idx_now or "探针" in idx_now, idx_now[:60])
    page_now = wiki_mod.read("跨层探针页")
    check("笔记写完立刻能 kb_read", "R75800H" in page_now, page_now[:40])

    # 文件名带日期前缀，模型给索引页起的标题可能完全不同 → 两种都要能解析
    hit_plain = wiki_mod.read("跨层探针页")
    check("去掉日期前缀也能定位", "R75800H" in hit_plain, hit_plain[:40])

    # 文件删掉后，refresh 要把索引里的死行清掉
    victims = list(wiki_mod.NOTES.glob("*跨层探针页*.md"))
    for v in victims:
        v.unlink()

    async def fake_sum2(msgs, max_tokens=None):
        return '{"title": "不该出现", "summary": "不该出现"}'
    core.complete_summary = fake_sum2
    await wiki_mod.refresh(core)
    check("删掉的页不留在索引里",
          all("跨层探针" not in p["path"] for p in wiki_mod.pages()),
          str([p["title"] for p in wiki_mod.pages()])[:60])

    # ---------- Z12. 提醒取消：连同派生的跟进一起取消 ----------
    soon = time.strftime("%Y-%m-%d %H:%M", time.localtime(time.time() + 10 * 86400))
    remind_mod.add("体检提醒一", soon)
    pend_r = remind_mod.list_pending(20)
    target = next((x for x in pend_r if x["text"] == "体检提醒一"), None)
    check("提醒能列出来", target is not None, str(len(pend_r)))
    if target:
        check("提醒派生了一条跟进",
              any(x["text"] == "体检提醒一" for x in followup_mod.pending(50)),
              str([x["text"] for x in followup_mod.pending(50)])[:50])
        msg_c = remind_mod.cancel(target["id"])
        check("取消提醒返回已取消", msg_c == "已取消", msg_c)
        check("取消后不在待提醒里",
              all(x["id"] != target["id"] for x in remind_mod.list_pending(20)))
        check("派生的跟进一起取消",
              all(x["text"] != "体检提醒一" for x in followup_mod.pending(50)),
              str([x["text"] for x in followup_mod.pending(50)])[:50])
    check("取消不存在的编号有提示", "没有这条" in remind_mod.cancel(999999),
          remind_mod.cancel(999999))

    # ---------- Z13. embedding 批量上限（103 块整批发 → 400）----------
    big_note = kb_mod.LEARN / "notes" / "2026-01-02-大页.md"
    big_note.write_text("\n\n".join(f"## 第{i}节\n\n" + "内容" * 150 for i in range(30)),
                        encoding="utf-8")
    batch_sizes = []

    async def fake_batch_embed(texts):
        batch_sizes.append(len(texts))
        return [[0.1, 0.2, 0.3] for _ in texts]

    real_embed = kb_mod._mem.embed
    kb_mod._mem.embed = fake_batch_embed
    try:
        n_idx = await kb_mod.index()
    finally:
        kb_mod._mem.embed = real_embed
    check("大文件会被拆成多批 embed",
          bool(batch_sizes) and max(batch_sizes) <= kb_mod.EMBED_BATCH,
          f"批次 {batch_sizes[:6]} 最大 {max(batch_sizes) if batch_sizes else 0}")
    check("多批之后块总数没丢", n_idx >= 30, str(n_idx))

    # 空字符串不能进 embedding 请求（智谱对空串直接 400）
    sent_inputs = []

    class _EmbedClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, **kw):
            sent_inputs.extend((kw.get("json") or {}).get("input") or [])

            class _R:
                status_code = 200

                def raise_for_status(self):
                    pass

                def json(self):
                    n = len((kw.get("json") or {}).get("input") or [])
                    return {"data": [{"index": i, "embedding": [0.5, 0.5]}
                                     for i in range(n)]}
            return _R()

    # 这个用例只验「空文本不进请求」，mock 返回的是 2 维假向量。
    # 写侧维度校验（embedding.dim=2048）是生产要的行为，不该为假向量放宽；
    # 只在这个实例上置 0，表示本用例不参与维度校验。
    _m = memory_mod.Memory()
    _m.dim = 0
    with patch("httpx.AsyncClient", _EmbedClient):
        res = await _m.embed(["", "  ", "正常文本"])
    check("空文本不进 embedding 请求", "" not in sent_inputs and "  " not in sent_inputs,
          str(sent_inputs))
    check("空文本返回空向量", res[0] == [] and res[1] == [] and res[2] == [0.5, 0.5],
          str(res))

    # ---------- Z14. 话题推进信号（不靠情感词）----------
    check("余弦 0.9 → repeat", mood_mod.classify_progress(0.9) == "repeat")
    check("余弦 0.7 → advance", mood_mod.classify_progress(0.7) == "advance")
    check("余弦 0.2 → switch", mood_mod.classify_progress(0.2) == "switch")
    check("余弦 0.5 落在死区", mood_mod.classify_progress(0.5) == "")

    vec_a = [1.0, 0.0, 0.0]
    same = fresh_mood(valence=0.2, energy=0.6, updated_at=1000.0)
    mood_mod.observe(same, "第一个问题", now=1000.0, vec=vec_a)
    v1, e1 = same["valence"], same["energy"]
    mood_mod.observe(same, "同一件事再问一遍", now=1060.0, vec=vec_a)
    check("同一个问题再问会掉心情", same["valence"] < v1, f"{v1:.3f} → {same['valence']:.3f}")
    check("同一个问题再问也掉精力", same["energy"] < e1, f"{e1:.3f} → {same['energy']:.3f}")

    sw = fresh_mood(updated_at=2000.0)
    mood_mod.observe(sw, "问题一", now=2000.0, vec=[1.0, 0.0, 0.0])
    before_v = sw["valence"]
    mood_mod.observe(sw, "换个话题问问别的", now=2100.0, vec=[0.0, 1.0, 0.0])
    check("换话题会涨心情", sw["valence"] > before_v, f"{before_v:.3f} → {sw['valence']:.3f}")

    first = fresh_mood(updated_at=2500.0)
    b0 = first["valence"]
    mood_mod.observe(first, "会话第一句", now=2500.0, vec=[1.0, 0.0, 0.0])
    check("没有上一句时不乱判", abs(first["valence"] - b0) < 0.06,
          f"{b0:.3f} → {first['valence']:.3f}")

    flo = fresh_mood(energy=0.05, updated_at=3000.0)
    mood_mod.observe(flo, "嗯", now=3000.0)
    check("精力有下限", flo["energy"] >= mood_mod.ENERGY_FLOOR - 1e-9, str(flo["energy"]))

    # 总结判定的 progress
    _d = summarize_mod.parse('{"summary":"s","key_facts":"他讨厌空话",'
                         '"topics":["求职"],"followups":"提醒他改简历","importance":0.5}')
    check("字符串 key_facts 不逐字拆", _d["key_facts"] == ["他讨厌空话"], str(_d["key_facts"]))
    check("字符串 followups 不逐字拆", _d["followups"] == ["提醒他改简历"], str(_d["followups"]))
    check("数组 topics 正常", _d["topics"] == ["求职"], str(_d["topics"]))

    check("总结能解析 progress",
          summarize_mod.parse('{"summary":"x","progress":"repeat"}')["progress"] == "repeat")
    check("非法 progress 被忽略",
          summarize_mod.parse('{"summary":"x","progress":"??"}')["progress"] == "")
    # 纪要跟记忆总结同一次调用产出，缺字段时留空由上层兜底
    check("总结能解析 digest",
          summarize_mod.parse('{"summary":"x","digest":" 旧事和新事\\n合成一段 "}')["digest"]
          == "旧事和新事 合成一段")
    check("没有 digest 就是空串",
          summarize_mod.parse('{"summary":"x"}')["digest"] == "")

    mood_mod.save(fresh_mood(valence=0.10))
    mood_mod.apply_progress("repeat")
    check("整段判定 repeat 落到状态", mood_mod.load()["valence"] < 0.10,
          str(mood_mod.load()["valence"]))
    mood_mod.save(fresh_mood(valence=0.00))
    mood_mod.apply_progress("switch")
    check("整段判定 switch 落到状态", mood_mod.load()["valence"] > 0.0,
          str(mood_mod.load()["valence"]))

    # ---------- Z15. 精力按任务难度消耗 ----------
    check("问一句答一句几乎不耗", mood_mod.task_cost(0, 30) == 0.0,
          str(mood_mod.task_cost(0, 30)))
    c_one = mood_mod.task_cost(1, 200)
    check("查一轮 + 长答案有成本", 0.005 < c_one <= 0.01, str(c_one))
    check("成本有上限", mood_mod.task_cost(6, 5000) <= 0.03,
          str(mood_mod.task_cost(6, 5000)))

    mood_mod.save(fresh_mood(energy=0.5))
    mood_mod.apply_task(3, 400)
    check("任务成本会扣精力", mood_mod.load()["energy"] < 0.5,
          str(mood_mod.load()["energy"]))
    mood_mod.save(fresh_mood(energy=0.05))
    mood_mod.apply_task(6, 5000)
    check("扣精力也守下限", mood_mod.load()["energy"] >= mood_mod.ENERGY_FLOOR - 1e-9,
          str(mood_mod.load()["energy"]))

    # tool_loop 要把轮数/工具数记进 stats，上层才能结算
    from types import SimpleNamespace as _NS

    loop_stats: dict = {}
    seq = {"n": 0}

    async def loop_complete(msgs, tools_arg=None, **kw):
        seq["n"] += 1
        if seq["n"] == 1:
            return {"content": "", "tool_calls": [
                {"id": "t1", "function": {"name": "kb_read", "arguments": "{}"}}]}
        return {"content": "好了", "tool_calls": []}

    async def loop_exec(*a, **kw):
        return "ok"

    fake_tools = _NS(TOOLS=[{"type": "function", "function": {"name": "kb_read"}}],
                     execute=loop_exec)
    out_loop = await chat_mod.tool_loop.run(
        chat_mod, [], complete=loop_complete, stream=loop_complete,
        tools_mod=fake_tools, tools_banned=set(), max_rounds=3,
        tool_timeout=5, total_timeout=10, stats=loop_stats)
    check("tool_loop 记了轮数与工具数",
          loop_stats.get("rounds") == 2 and loop_stats.get("tools") == 1,
          f"{loop_stats} / {out_loop}")

    await pacing.run()

    # ---------- Z17. 同轮工具并发（只读并行、写串行）----------
    check("写操作不在只读名单里",
          not ({"note_write", "skill_save", "meme_send", "remind_me", "remind_cancel"}
               & tools_module.READ_ONLY_TOOLS),
          str(sorted(tools_module.READ_ONLY_TOOLS))[:40])

    async def timed_exec(core, name, args, images=None):
        await asyncio.sleep(0.15)
        return name + "-ok"

    def make_complete(rounds):
        it = iter(rounds)

        async def _c(work, tools_arg=None):
            try:
                return next(it)
            except StopIteration:
                return {"content": "完了", "tool_calls": []}
        return _c

    two_search = {"content": "", "tool_calls": [
        {"id": "a", "function": {"name": "web_search", "arguments": '{"query":"x"}'}},
        {"id": "b", "function": {"name": "web_search", "arguments": '{"query":"y"}'}}]}
    fake_par = _NS(
        TOOLS=[{"type": "function", "function": {"name": "web_search"}}],
        execute=timed_exec, READ_ONLY_TOOLS={"web_search"})
    t0 = time.perf_counter()
    await chat_mod.tool_loop.run(
        chat_mod, [], complete=make_complete([two_search]),
        stream=make_complete([two_search]), tools_mod=fake_par, tools_banned=set(),
        max_rounds=2, tool_timeout=5, total_timeout=5, stats={})
    par_s = time.perf_counter() - t0
    check("同轮只读工具并发执行", par_s < 0.26, f"{par_s:.2f}s")

    two_write = {"content": "", "tool_calls": [
        {"id": "a", "function": {"name": "note_write", "arguments": '{"title":"a"}'}},
        {"id": "b", "function": {"name": "note_write", "arguments": '{"title":"b"}'}}]}
    fake_seq = _NS(
        TOOLS=[{"type": "function", "function": {"name": "note_write"}}],
        execute=timed_exec, READ_ONLY_TOOLS={"web_search"})
    t0 = time.perf_counter()
    await chat_mod.tool_loop.run(
        chat_mod, [], complete=make_complete([two_write]),
        stream=make_complete([two_write]), tools_mod=fake_seq, tools_banned=set(),
        max_rounds=2, tool_timeout=5, total_timeout=5, stats={})
    seq_s = time.perf_counter() - t0
    check("写操作串行执行", seq_s >= 0.29, f"{seq_s:.2f}s")

    # 同一轮里重复的调用只执行一次
    dup = {"content": "", "tool_calls": [
        {"id": "a", "function": {"name": "web_search", "arguments": '{"query":"z"}'}},
        {"id": "b", "function": {"name": "web_search", "arguments": '{"query":"z"}'}}]}
    ran = {"n": 0}

    async def count_exec(core, name, args, images=None):
        ran["n"] += 1
        return "ok"

    fake_dup = _NS(
        TOOLS=[{"type": "function", "function": {"name": "web_search"}}],
        execute=count_exec, READ_ONLY_TOOLS={"web_search"})
    await chat_mod.tool_loop.run(
        chat_mod, [], complete=make_complete([dup]), stream=make_complete([dup]),
        tools_mod=fake_dup, tools_banned=set(), max_rounds=2,
        tool_timeout=5, total_timeout=5, stats={})
    check("同轮重复调用只跑一次", ran["n"] == 1, str(ran))

    # 清单外的已注册工具不能因为模型点了名就执行。
    banned_calls: dict[str, int] = {}

    async def count_banned(core, name, args, images=None):
        banned_calls[name] = banned_calls.get(name, 0) + 1
        return "secret"

    sneak = {"content": "", "tool_calls": [
        {"id": "a", "function": {"name": "memory_read", "arguments": "{}"}},
        {"id": "b", "function": {"name": "web_search", "arguments": '{"query":"ok"}'}},
    ]}
    fake_ban = _NS(
        TOOLS=[{"type": "function", "function": {"name": "web_search"}}],
        execute=count_banned, READ_ONLY_TOOLS={"web_search", "memory_read"})
    await chat_mod.tool_loop.run(
        chat_mod, [], complete=make_complete([sneak]), stream=make_complete([sneak]),
        tools_mod=fake_ban, tools_banned={"memory_read"}, max_rounds=2,
        tool_timeout=5, total_timeout=5, stats={})
    check("清单外工具处理函数调用次数为零",
          banned_calls.get("memory_read", 0) == 0
          and banned_calls.get("web_search", 0) == 1,
          str(banned_calls))

    # 记忆库初始化要自己建父目录：空数据根按 README 启动会打不开库。
    from app_core import memory as _mem_mod
    _missing_db = TMP / "fresh-root" / "qqbot-data" / "livingmemory.db"
    _old_db = _mem_mod.DB
    _mem_mod.DB = _missing_db
    try:
        _mem_mod.Memory().init()
        check("记忆库初始化创建父目录",
              _missing_db.is_file() and _missing_db.parent.is_dir())
    finally:
        _mem_mod.DB = _old_db

    from app_core import inflight as _inflight
    _inflight.mark("private:111")
    check("在途轮次被记下来", _inflight.busy("private:111"))
    _inflight.clear("private:111")
    check("轮次结束后在途标记清除", not _inflight.busy("private:111"))

    _prev_followup_db = followup_store_mod.DB
    followup_store_mod.DB = TMP / "followup-quote-test.db"
    try:
        _contract = "核对租房合同"
        _migration = "迁移数据库脚本"
        followup_mod.add(_contract)
        followup_mod.add(_migration)
        _contract_question = "核对租房合同了吗？"
        followup_mod.note_asked(_contract_question)
        followup_mod.note_asked("迁移数据库脚本写完了吗？")
        _transition = followup_mod.answer_turn(
            "搞定了。", "private:quote-test", quote_text=_contract_question)
        check("引用旧问题只关闭引用事项",
              _transition.get("done") == [followup_store_mod.id_for_text(_contract)]
              and any(x["text"] == _migration for x in followup_mod.pending(100)),
              str(_transition))
        for _unrelated in ("搜下天气", "我今天有点累", "换个话题"):
            check("短句换题不算回应 " + _unrelated,
                  not followup_mod.responds_to(_contract, _contract_question, _unrelated))
        check("省略主题的时间答复仍能入账",
              followup_mod.responds_to("晚上去签租房合同", "晚上签约去吗？", "上完班再去"))
    finally:
        followup_store_mod.DB = _prev_followup_db

    # 还原测试期替换掉的真实现
    chat_mod.tools.execute = REAL_TOOLS_EXECUTE
