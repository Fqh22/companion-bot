"""离线回归组，跨组状态由入口显式传递。"""
from offline_regression.fixtures import (
    json, os, sqlite3, time, datetime, timedelta, TMP, chat_mod, config_mod, followup_mod,
    kb_mod, learn_mod, mood_mod, remind_mod, REAL_TOOLS_EXECUTE, check,
)
from offline_regression import adapters, pacing


async def run(core, mem, wiki_mod):
    # ---------- Z3. 配置自检 / 分档阈值 / 归档恢复 / 向量与文本去重 ----------
    # admin.user_id 留空是「不报错但全哑」的失败模式，必须能被检测到
    real_uid = core.admin_uid
    core.admin_uid = ""
    check("admin 没配时自检报出来",
          any("admin.user_id" in m for m in core.config_issues()),
          str(core.config_issues())[:40])
    check("admin 没配时状态行带警告", "未配置" in core.config_status())
    core.platform_owner_sessions = {"feishu:ou_test"}
    check("仅平台主人时自检不误报管理员缺失",
          not any("admin.user_id" in m for m in core.config_issues()),
          str(core.config_issues())[:60])
    core.platform_owner_sessions = set()
    _saved_cache = config_mod._cache
    config_mod._cache = {**config_mod.load(),
                         "admin": {"user_id": "0", "identities": []},
                         "feishu": {"enabled": True, "owner_open_id": "ou_cfg"}}
    check("config.owner_session 支持仅飞书主人",
          config_mod.owner_session() == "feishu:ou_cfg",
          config_mod.owner_session())
    config_mod._cache = _saved_cache
    core.admin_uid = real_uid or "10001"
    check("admin 配了就不报警", not core.config_issues(), str(core.config_issues()))

    # 配置/人格读失败必须留痕：两者都会「静默降级」——机器人还活着，但已经不是它了
    import app_core.config as _cfg
    _real_path, _real_cache = _cfg.CONFIG_PATH, _cfg._cache
    _bad = TMP / "bad-config.yaml"
    _bad.write_text("admin:\n  user_id: 1\n   bad: 2\n", encoding="utf-8")
    _cfg.CONFIG_PATH = _bad
    _cfg.load(force=True)
    check("config 解析失败被记下来", bool(_cfg.load_error()), _cfg.load_error()[:60])
    check("config 解析失败进自检",
          any("config.yaml" in m for m in core.config_issues()),
          str(core.config_issues())[:80])
    from app_core import audit as _audit_bad
    _raised = False
    try:
        _audit_bad.requires_approval("note_write")
    except _audit_bad.ApprovalPolicyError:
        _raised = True
    check("配置损坏时审批策略不算关闭", _raised)
    # 前面的超时用例把 tools.execute 换成了慢桩；这里要测真入口。
    _blocked = await REAL_TOOLS_EXECUTE(
        core, "note_write", {"title": "x", "content": "y"})
    check("配置损坏时写操作不执行",
          "没有执行" in (_blocked or ""), str(_blocked)[:80])
    _cfg.CONFIG_PATH = _real_path
    _cfg.load(force=True)
    check("配置恢复后错误清空", not _cfg.load_error(), _cfg.load_error()[:60])

    # 改了文件没重启：必须能被看出来（否则「改了阈值怎么没变」只能靠猜）
    _tmpcfg = TMP / "cfg-stale.yaml"
    _tmpcfg.write_text("admin:\n  user_id: '1'\n", encoding="utf-8")
    _real_cfg = _cfg.CONFIG_PATH
    _cfg.CONFIG_PATH = _tmpcfg
    _cfg.load(force=True)
    check("刚加载不算过期", not _cfg.stale())
    os.utime(_tmpcfg, (_tmpcfg.stat().st_mtime + 5,) * 2)
    check("改了没重启能看出来", _cfg.stale())
    _cfg.CONFIG_PATH = _real_cfg
    _cfg.load(force=True)
    check("重新加载后不再提示", not _cfg.stale())

    # 月度维护：老日志截断 + VACUUM（不做也不会立刻坏，做了一年省几十 MB）
    from app_core import maintenance as maint_mod
    _ml = TMP / "maint.jsonl"
    _ml.write_text(
        json.dumps({"ts": time.time() - 40 * 86400}) + "\n"
        + json.dumps({"ts": time.time() - 1}) + "\n"
        + "坏行\n", encoding="utf-8")
    _mr = maint_mod.trim_jsonl(_ml, 30)
    check("维护：老日志和坏行被截掉", _mr["dropped"] == 2 and _mr["kept"] == 1, str(_mr))
    check("维护：文件真的变小", _mr["after"] < _mr["before"], str(_mr))
    _mdb = TMP / "maint.db"
    _c0 = sqlite3.connect(_mdb)
    _c0.executescript("create table t(x); insert into t values(1);")
    _c0.commit(); _c0.close()
    _mv = maint_mod.vacuum(_mdb)
    check("维护：VACUUM 成功", _mv["ok"], str(_mv))
    check("维护：不存在的库不炸", not maint_mod.vacuum(TMP / "nope.db")["ok"])

    # 运维状态进周报：异地备份、恢复演练
    _opsd = TMP / "ops"
    _opsd.mkdir(exist_ok=True)
    (_opsd / "offsite.state").write_text(
        json.dumps({"status": "unconfigured", "ts": time.time()}), encoding="utf-8")
    (_opsd / "restore.state").write_text(
        json.dumps({"ok": True, "ts": time.time(), "dbs": [{"ok": True}]}),
        encoding="utf-8")
    _ol = maint_mod.ops_status(_opsd)
    check("运维状态：异地未配置会被指出来",
          any("未配置" in x for x in _ol), str(_ol))
    check("运维状态：演练通过会说几天前",
          any("恢复演练" in x and "通过" in x for x in _ol), str(_ol))
    (_opsd / "restore.state").write_text(
        json.dumps({"ok": False, "ts": time.time(), "errors": ["chat.db 校验失败"]}),
        encoding="utf-8")
    check("运维状态：演练失败会报出来",
          any("失败" in x for x in maint_mod.ops_status(_opsd)))

    # prompt 快照：以前只存 sha+长度，等于排障时什么也看不到
    await core.init_db()
    await core._log_prompt("private:probe",
                           [{"role": "user", "content": "探针内容 ABC"}], digest=True)
    _pl = await core.last_prompts(1)
    check("prompt 快照存了正文",
          bool(_pl) and "探针内容 ABC" in _pl[0]["body"], str(_pl)[:100])
    check("prompt 快照带标记", bool(_pl) and "digest" in _pl[0]["flags"],
          str(_pl)[:100])
    # 分块账：不记这个，就答不上「每轮 prompt 里各块各占多少」
    _blk = chat_mod.prompt_blocks([
        {"role": "system", "content": "[你记得的事]\n- 甲"},
        {"role": "user", "content": [{"type": "text", "text": "图"}]}])
    check("分块账：带上可辨认的标签",
          _blk[0]["label"].startswith("[你记得的事]"), str(_blk[0]))
    check("分块账：多模态按序列化长度算",
          _blk[1]["chars"] > 0 and _blk[1]["label"] == "[多模态]", str(_blk[1]))
    check("快照里存了分块账",
          bool(_pl) and bool(_pl[0].get("blocks"))
          and _pl[0]["blocks"][0]["chars"] > 0,
          str(_pl[0].get("blocks"))[:120])
    _real_perr = chat_mod._PERSONA_ERROR
    chat_mod._PERSONA_ERROR = "system_prompt 为空"
    check("人格异常进自检",
          any("人格" in m for m in core.config_issues()),
          str(core.config_issues())[:80])
    chat_mod._PERSONA_ERROR = _real_perr
    check("人格正常时不报警", not core.config_issues(), str(core.config_issues())[:80])

    # QQBOT_ROOT：运行时路径都从它派生，默认 /opt（换目录不用改代码）
    import importlib
    _paths_mod = importlib.import_module("app_core.paths")
    _old_root = os.environ.get("QQBOT_ROOT")
    os.environ["QQBOT_ROOT"] = str(TMP / "root")
    try:
        importlib.reload(_paths_mod)
        check("QQBOT_ROOT 改根后路径跟着变",
              str(_paths_mod.DATA) == str(TMP / "root" / "nonebot" / "data"),
              str(_paths_mod.DATA))
        check("QQBOT_ROOT 下 keys 与记忆库都在根内",
              str(_paths_mod.KEYS).startswith(str(TMP / "root"))
              and str(_paths_mod.MEM_DB).startswith(str(TMP / "root")),
              str(_paths_mod.MEM_DB))
    finally:
        if _old_root is None:
            os.environ.pop("QQBOT_ROOT", None)
        else:
            os.environ["QQBOT_ROOT"] = _old_root
        importlib.reload(_paths_mod)


    # 命令清单：/help 的唯一真相源（lint 会核对注册表，这里只保证内容可用）
    from app_core import help as help_mod
    check("help 清单够全（展示+隐藏）",
      len(help_mod.all_names()) >= 29, str(len(help_mod.all_names())))
    # 2026-11-16 批次 4：新增 /audit 与 /approve 两条审计门面（DEBUG 组），
    # 展示数 12 → 14。阈值跟着调到 14，仍然卡住「清单不许继续膨胀」。
    check("展示的清单已精简", len(help_mod.names()) <= 14, str(len(help_mod.names())))
    check("help 无重复", len(help_mod.names()) == len(set(help_mod.names())))
    check("help 文案分组", "【日常】" in help_mod.text() and "/ping" in help_mod.text())

    # 启动补偿：没有管理员时不该动
    core.admin_uid = ""
    r_catch = await core.catch_up_summary()
    check("没有管理员就不做启动补偿", r_catch["reason"] == "no-admin", str(r_catch))
    core.admin_uid = real_uid

    # 表情包阈值按情绪分档：正向最容易、负向次之、中性最严
    real_thr = core.meme_thresholds
    core.meme_thresholds = {"positive": 0.15, "neutral": 0.20, "negative": 0.18}
    check("正向 0.16 就发表情", core.meme_allowed({"valence": 0.16, "energy": 0.7}))
    check("吐槽 -0.19 也能发（原来被 0.20 卡住）",
          core.meme_allowed({"valence": -0.19, "energy": 0.7}))
    check("中性 0.19 不发", not core.meme_allowed({"valence": 0.0, "energy": 0.89}))
    core.meme_thresholds = real_thr

    # 归档 → 按语义恢复（cleanup 是软删，向量还在）
    def _doc_count() -> int:
        con = sqlite3.connect(TMP / "livingmemory.db")
        n = con.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        con.close()
        return n

    probe_vec = [1.0, 0.0, 0.0]
    doc_gone = mem.add_document(text="他明年想去日本看樱花", session="private:restore",
                                importance=0.9, topics=[], key_facts=[],
                                vec=probe_vec, range_key="restore:1")
    con = sqlite3.connect(TMP / "livingmemory.db")
    row_r = con.execute("SELECT id, metadata FROM documents WHERE doc_id=?",
                        (doc_gone,)).fetchone()
    meta_r = json.loads(row_r[1])
    meta_r["status"] = "archived"
    with con:
        con.execute("UPDATE documents SET metadata=? WHERE id=?",
                    (json.dumps(meta_r, ensure_ascii=False), row_r[0]))
    con.close()
    got_back = mem.restore(probe_vec, top_k=3)
    check("归档的记忆能按语义恢复",
          any(x["doc_id"] == doc_gone for x in got_back), str(len(got_back)))
    con = sqlite3.connect(TMP / "livingmemory.db")
    st = json.loads(con.execute("SELECT metadata FROM documents WHERE doc_id=?",
                                (doc_gone,)).fetchone()[0]).get("status")
    con.close()
    check("恢复后状态回到 active", st == "active", str(st))
    check("无关查询恢复不了东西", mem.restore([0.0, 0.0, 0.0]) == [])

    # 相似向量仍可存两条，自动去重只处理完全相同的文本或批次。
    n0 = _doc_count()
    mem.add_document(text="另一条有相同向量的记忆", session="private:shadow",
                     importance=0.5, topics=[], key_facts=[], vec=probe_vec,
                     range_key="shadow:probe")
    check("相似向量不会自动合并", _doc_count() == n0 + 1)
    check("停止采集新的碎片整合候选",
          not (TMP / "root" / "nonebot" / "data" / "consolidation-candidates.jsonl").exists())

    # ---------- Z4. 知识库治理：来源 / 最后验证 / 状态 ----------
    # 核心风险：笔记全是模型自己写的，它读的时候分不出「原始输入」和「我上次编的」
    learn_mod.NOTES = wiki_mod.NOTES
    learn_mod.note_write("K8s 排障清单", "先看 pod 状态，再看 events。", tag="运维")
    p_k8s = sorted(wiki_mod.NOTES.glob("*K8s*.md"))[-1]
    txt_note = p_k8s.read_text(encoding="utf-8")
    check("新笔记头带来源与验证字段",
          "- 来源：" in txt_note and "- 最后验证：未验证" in txt_note,
          txt_note[:60].replace("\n", " "))
    meta_n = learn_mod.note_meta(txt_note)
    check("来源默认自撰", meta_n["source"] == "自撰", str(meta_n["source"]))
    check("默认未验证（时间戳为 0）", meta_n["verified_ts"] == 0.0)
    check("标签是未验证", learn_mod.note_label(meta_n) == "【自撰·未验证】",
          learn_mod.note_label(meta_n))

    learn_mod.set_meta(p_k8s, verified="2026-11-01 15:00", status="active")
    learn_mod.set_meta(p_k8s, verified="2026-11-01 16:00")
    txt_after = p_k8s.read_text(encoding="utf-8")
    check("set_meta 幂等不重复插字段", txt_after.count("- 最后验证：") == 1,
          str(txt_after.count("- 最后验证：")))
    check("set_meta 改到新值",
          learn_mod.note_meta(txt_after)["verified_ts"] > 0)
    check("已验证标签", "已验证" in learn_mod.note_label(learn_mod.note_meta(txt_after)),
          learn_mod.note_label(learn_mod.note_meta(txt_after)))

    learn_mod.set_meta(p_k8s, status="已废弃")
    check("废弃标签",
          learn_mod.note_label(
              learn_mod.note_meta(p_k8s.read_text(encoding="utf-8"))) == "【已废弃】")

    # 索引：带标签、废弃页不出现
    con = sqlite3.connect(TMP / "chat.db")
    with con:
        con.execute("INSERT OR REPLACE INTO wiki_pages"
                    "(path, title, summary, project, mtime, ts, source, verified_ts, status) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (str(p_k8s), "K8s 排障清单", "先看 pod 状态", "运维",
                     time.time(), time.time(), "自撰", 0.0, "active"))
        con.execute("INSERT OR REPLACE INTO wiki_pages"
                    "(path, title, summary, project, mtime, ts, source, verified_ts, status) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    ("/x/old.md", "旧结论", "已经不对了", "运维",
                     time.time(), time.time(), "自撰", 0.0, "已废弃"))
    con.close()
    idx_kb = wiki_mod.index_text()
    check("索引带可信度标签", "【自撰·未验证】" in idx_kb, idx_kb[:80])
    check("废弃页不进索引", "旧结论" not in idx_kb, idx_kb[:80])

    page_kb = wiki_mod.read("K8s 排障清单")
    check("kb_read 头部报来源与验证", "来源：" in page_kb[:120] and "最后验证" in page_kb[:120],
          page_kb[:60].replace("\n", " "))

    # raw/ 是对话原文（原始输入），默认来源不能算成「自撰」
    raw_dir = TMP / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    p_raw = raw_dir / "2026-11-01.md"
    p_raw.write_text("# 2026-11-01 对话原文\n\n[10:00] 我：原话。\n", encoding="utf-8")
    check("raw/ 按对话原文算来源", learn_mod.page_label(p_raw) == "【原文·未验证】",
          learn_mod.page_label(p_raw))

    # 网页笔记要能回溯到原地址（否则无法验证，只能当二手转述）
    learn_mod.note_write("网页-某篇", "要点一二三", tag="网页学习",
                         source="网页 https://example.com/a（2026-11-01 15:00 抓取）")
    p_web = sorted(wiki_mod.NOTES.glob("*网页-某篇*.md"))[-1]
    check("网页笔记存了原地址",
          "example.com" in p_web.read_text(encoding="utf-8"))

    # ---------- Z5. 知识库周检：死页 / 超期 / 超长 / 重复 ----------
    now_ts = time.time()
    long_text = ("# 长页\n\n开头讲的是老结论。" + "填" * 2100
                 + "\n\n## 2026-10-01 10:00 补充（未验证）\n\n最后这条才是新结论。\n")
    p_long = wiki_mod.NOTES / "2026-01-01-长页.md"
    p_long.write_text(long_text, encoding="utf-8")
    din = wiki_mod._digest_input(long_text)
    check("摘要输入长度受控", len(din) <= 1600, str(len(din)))
    check("摘要输入带得动页尾的新结论", "最后这条才是新结论" in din)
    check("摘要输入保留开头", din.startswith("# 长页"))

    rows_z5 = [
        # (path, title, summary, project, mtime, source, verified_ts, status, last_open)
        (str(p_long), "长页", "很长的页", "运维", now_ts - 100 * 86400, "自撰", 0.0, "active", 0.0),
        ("/x/2026-09-01-死页.md", "死页", "没人读", "运维", now_ts - 400 * 86400, "自撰",
         now_ts - 400 * 86400, "active", now_ts - 400 * 86400),
        ("/x/2026-09-02-旧结论A.md", "旧结论A", "验证过很久了", "运维", now_ts - 200 * 86400,
         "自撰", now_ts - 200 * 86400, "active", now_ts),
        ("/x/2026-01-01-装机.md", "装机清单", "a", "硬件", now_ts, "自撰", now_ts, "active", now_ts),
        ("/x/2026-02-01-装机.md", "装机清单二", "b", "硬件", now_ts, "自撰", now_ts, "active", now_ts),
        ("/x/2026-03-01-废弃.md", "废弃页", "c", "运维", now_ts, "自撰", 0.0, "已废弃", 0.0),
    ]
    con = sqlite3.connect(TMP / "chat.db")
    with con:
        con.execute(
            "INSERT OR REPLACE INTO wiki_pages"
            "(path, title, summary, project, mtime, ts, source, verified_ts, status, last_open_ts) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (str(p_long), "长页", "很长的页", "运维", now_ts - 100 * 86400, now_ts,
             "自撰", 0.0, "active", 0.0))
        for path, title, summ, proj, mt, src, vts, st, lo in rows_z5[1:]:
            con.execute(
                "INSERT OR REPLACE INTO wiki_pages"
                "(path, title, summary, project, mtime, ts, source, verified_ts, status, last_open_ts) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (path, title, summ, proj, mt, now_ts, src, vts, st, lo))
    con.close()

    h = wiki_mod.health()
    titles = lambda key: [p["title"] for p in h[key]]          # noqa: E731
    check("周检：发现死页", "死页" in titles("dead"), str(titles("dead")))
    check("周检：发现超期未复核", "旧结论A" in titles("stale"), str(titles("stale")))
    check("周检：发现超长页", "长页" in titles("long"), str(titles("long")))
    check("周检：发现重复 slug", any("装机" in k for k in h["dup"]),
          str(list(h["dup"])[:3]))
    check("周检：废弃页不进任何统计",
          all("废弃页" not in titles(k) for k in ("dead", "stale", "unverified", "long")))
    check("周检：溢出量算得对",
          h["overflow"] == max(0, h["total"] - wiki_mod.INDEX_MAX_PAGES), str(h["overflow"]))

    wt = wiki_mod.weekly_text(2)
    check("周检文本含标题和校对命令", "知识库周检" in wt and "/kb_verify" in wt, wt[:40])
    picks = wiki_mod.audit_sample(2)
    check("抽样排除废弃页", all(p["title"] != "废弃页" for p in picks),
          str([p["title"] for p in picks]))
    check("抽样优先未验证的页", float(picks[0]["verified_ts"] or 0) == 0 if picks else False,
          str([(p["title"], p["verified_ts"]) for p in picks]))

    # kb_read 要记「最后打开时间」，否则死页算不出来
    before_open = next(p["last_open_ts"] for p in wiki_mod.pages()
                       if p["path"] == str(p_k8s))
    wiki_mod.read("K8s 排障清单")
    after_open = next(p["last_open_ts"] for p in wiki_mod.pages()
                      if p["path"] == str(p_k8s))
    check("kb_read 记最后打开时间", after_open > before_open,
          f"{before_open} -> {after_open}")

    # ---------- Z6. 情绪动力学：状态 vs 计数器 ----------
    def fresh_mood(**kw):
        m = {"valence": 0.0, "valence_base": 0.0, "energy": 0.7, "social": 0.5,
             "updated_at": time.time(), "last_user_ts": 0.0, "why": []}
        m.update(kw)
        return m

    # 1) 衰减可复合：一次 10 小时 == 两次 5 小时（旧的 min(0.9,0.12h) 做不到）
    a = fresh_mood(valence=0.8)
    b = fresh_mood(valence=0.8, updated_at=a["updated_at"])
    mood_mod.settle(a, a["updated_at"] + 10 * 3600)
    mood_mod.settle(b, b["updated_at"] + 5 * 3600)
    mood_mod.settle(b, b["updated_at"] + 5 * 3600)
    check("衰减可复合（与 settle 次数无关）", abs(a["valence"] - b["valence"]) < 1e-9,
          f"{a['valence']:.4f} vs {b['valence']:.4f}")

    # 2) 越极端越难推动
    hi = fresh_mood(valence=0.9)
    mid = fresh_mood(valence=0.1)
    mood_mod._bump(hi, "valence", -0.10)
    mood_mod._bump(mid, "valence", -0.10)
    check("极端值难推动（不是线性计数）", (hi["valence"] - 0.9) > (mid["valence"] - 0.1),
          f"Δ {hi['valence'] - 0.9:+.3f} vs {mid['valence'] - 0.1:+.3f}")

    # 3) 衰减回基线，不是回 0
    s_base = fresh_mood(valence=0.6, valence_base=0.5)
    mood_mod.settle(s_base, s_base["updated_at"] + 30 * 3600)
    check("衰减回基线而不是 0", s_base["valence"] > 0.35,
          f"{s_base['valence']:.3f}")

    # 4) 连发比零星消息更耗精力
    t0 = time.time()
    burst = fresh_mood(updated_at=t0)
    for i in range(20):
        mood_mod.observe(burst, "在吗", now=t0 + i * 3)
    loner = fresh_mood(updated_at=t0)
    for i in range(20):
        mood_mod.observe(loner, "在吗", now=t0 + i * 7200)
    check("连发比零星聊更耗精力", (0.7 - burst["energy"]) > 3 * (0.7 - loner["energy"]),
          f"掉 {0.7 - burst['energy']:.2f} vs {0.7 - loner['energy']:.2f}")

    # 5) 热度不会 13 条就封顶
    soc = fresh_mood(updated_at=t0)
    for i in range(15):
        mood_mod.observe(soc, "嗯", now=t0 + i)
    check("热度 15 条仍未封顶", soc["social"] < 0.99, f"{soc['social']:.3f}")

    # 6) 状态行给的是数值 + 原因，不是一个标签
    dsc = mood_mod.describe({"valence": 0.42, "valence_base": 0.1, "energy": 0.6,
                             "social": 0.7, "why": [{"text": "他说了「谢谢」"}]})
    check("状态行含原始数值", "+0.42" in dsc and "0.60" in dsc, dsc[:50])
    check("状态行含原因", "谢谢" in dsc, dsc[-40:])

    # 7) 事件化：超时/被纠正会留下痕迹
    ev = fresh_mood(valence=0.2)
    e0, v0 = ev["energy"], ev["valence"]
    mood_mod.observe_event(ev, "timeout")
    check("超时会掉精力与心情", ev["energy"] < e0 and ev["valence"] < v0,
          f"{ev['energy']:.3f}/{ev['valence']:.3f}")
    check("事件留下原因", any("超时" in x["text"] for x in ev["why"]),
          str([x["text"] for x in ev["why"]])[:40])
    mood_mod.observe(ev, "不对吧，你记错了", now=time.time() + 60)
    check("被纠正会再掉心情", ev["valence"] < v0,
          f"{ev['valence']:.3f}")

    # 8) 表情包判据：累不是情绪强
    check("累不再被当成情绪强烈", not core.meme_allowed({"valence": 0.0, "energy": 0.3}))
    check("兴奋 + 心情好才会发", core.meme_allowed({"valence": 0.2, "energy": 0.9}))

    # ---------- Z7. 情绪历史快照 ----------
    # 云端跑的时候 TMP/chat.db 是生产库副本，里面已经有真实的 mood_history 行；
    # 先清空，断言才能只针对本测试写的那几天（本地空库还没有这张表，忽略即可）。
    try:
        con = sqlite3.connect(TMP / "chat.db")
        with con:
            con.execute("DELETE FROM mood_history")
        con.close()
    except sqlite3.OperationalError:
        pass
    snap_s = fresh_mood(valence=0.3, valence_base=0.2, energy=0.6, social=0.7,
                        msgs_today=12, events_today=2)
    mood_mod.snapshot(snap_s, day="2026-10-30")
    mood_mod.snapshot(snap_s, day="2026-10-30")          # 同一天重复：覆盖，不堆行
    rows_s = mood_mod.recent(7)
    check("同日快照只留一行",
          len([r for r in rows_s if r["day"] == "2026-10-30"]) == 1, str(len(rows_s)))

    mood_mod.snapshot(fresh_mood(valence=-0.4, valence_base=-0.2, energy=0.4,
                                 social=0.3, msgs_today=3), day="2026-10-31")
    mood_mod.snapshot(snap_s, day="2026-11-01")
    rows_s = mood_mod.recent(2)
    check("快照按时间正序返回", rows_s and rows_s[-1]["day"] == "2026-11-01",
          str([r["day"] for r in rows_s]))

    sm = mood_mod.summary(3)
    check("情绪均值算得对", abs(sm["valence"] - (0.3 - 0.4 + 0.3) / 3) < 1e-3,
          str(sm["valence"]))
    check("能找出最好/最差的一天",
          sm["best"]["day"] == "2026-10-30" and sm["worst"]["day"] == "2026-10-31",
          f"{sm['best']['day']} / {sm['worst']['day']}")

    # 当日计数：跨天要清零，否则快照里的「消息数」会一直累加
    cnt = fresh_mood()
    t_cnt = time.time()
    mood_mod.observe(cnt, "在吗", now=t_cnt)
    check("当天消息计数 +1", cnt["msgs_today"] == 1, str(cnt["msgs_today"]))
    mood_mod.observe(cnt, "在吗", now=t_cnt + 86400)
    check("跨天计数清零后重新计", cnt["msgs_today"] == 1,
          f"day={cnt['day']} msgs={cnt['msgs_today']}")
    mood_mod.observe_event(cnt, "timeout", now=t_cnt + 86400 + 60)
    check("事件也有当日计数", cnt["events_today"] == 1, str(cnt["events_today"]))

    # ---------- Z8. 输入聚合队列 / 情绪配置化 / 原文归档 ----------
    from app_core import turn as turn_mod

    f1 = turn_mod.Turn(file=("报错.log", "堆栈内容" * 100, "文本"))
    f1.merge(turn_mod.Turn(texts=["帮我看看"]))
    prompt_f, stored_f = f1.compose()
    check("文件轮：正文含文件内容与补充说明",
          "报错.log" in prompt_f and "帮我看看" in prompt_f, prompt_f[:60])
    check("文件轮：摘要里也带上补充那句",
          stored_f is not None and "帮我看看" in stored_f and len(stored_f) < 500,
          str(stored_f)[:60])

    v1 = turn_mod.Turn(texts=["明天几点开会"], meta={"voice": True})
    vp, vs = v1.compose()
    check("语音轮：正文带 [语音] 前缀", vp == "[语音] 明天几点开会", vp)
    check("语音轮：摘要只记一份（不再追加「补充」）",
          vs == "[语音] 明天几点开会", str(vs))

    check("只有图片时给占位正文",
          turn_mod.Turn(images=["/tmp/a.jpg"]).compose()[0] == "[发了图片]")

    t3 = turn_mod.Turn()
    t3.merge(turn_mod.Turn(texts=["第一句"]))
    t3.merge(turn_mod.Turn(texts=["第二句"]))
    check("连续两条文字按顺序合并", t3.texts == ["第一句", "第二句"], str(t3.texts))

    await adapters.run()

    # ---------- 时效：知识保质期 / 自主搜索规则 / 话题巡检 ----------
    from app_core import prompt as prompt_fresh
    fh = prompt_fresh.freshness_hint()
    check("时效规则说明了知识有截止时间", "截止时间" in fh, fh[:40])
    check("时效规则授权它自己搜", "自己搜" in fh, fh[:60])
    check("时效规则要求带日期", "日期" in fh, fh[-40:])

    # ---- 风格块：五条提示合成一条，冲突在一处解决 ----
    _sb = prompt_fresh.style_block
    b1 = _sb("好累", [], {}, [], burst=1)
    check("情绪短句给事实不给规则", "在说自己的状态" in b1 and "别给建议" not in b1, b1)
    check("情绪短句不含时效规则", "时效" not in b1, b1)
    check("情绪短句不含填充式形式", "只做一件事" not in b1, b1)
    check("信号只有一段", b1.count("【这一轮】") == 1, b1)
    b2 = _sb("这报错怎么改", [], {}, [], images=1)
    check("求助图保留一句保底", "图里可能是报错" in b2, b2)
    b3 = _sb("哈哈", [], {}, [], images=1)
    check("分享图不给保底", "报错" not in b3 and "附了 1 张图" in b3, b3)
    b_emoji = _sb("😂", [], {}, [])
    check("纯表情只给事实", "他这条 1 字" in b_emoji and "别分析" not in b_emoji, b_emoji)
    b_react = _sb("你是不是又偷偷看我了", [], {}, [])
    check("短推论只给事实", "是在问你" in b_react, b_react)
    check("表情识别", prompt_fresh.is_reaction_turn("😂"))
    check("括号表情识别", prompt_fresh.is_reaction_turn("[捂脸]"))
    check("短推论识别", prompt_fresh.is_reaction_turn("你是不是又看我了"))
    check("征求意见不按反应处理",
          not prompt_fresh.is_reaction_turn("你觉得我该不该辞职"))
    check("求助图不按反应处理",
          not prompt_fresh.is_reaction_turn("这报错怎么改", images=1, image_fix=True))
    b5 = _sb("为什么 SQLite 的 WAL 模式在多进程下这么容易锁住，我不太理解它的机制",
             [{"role": "assistant", "content": "对，" + "很长" * 40},
              {"role": "assistant", "content": "嗯，" + "很长" * 40}], {})
    check("长问题只给事实", "是在问你" in b5 and "先说结论" not in b5, b5)
    _mf = prompt_fresh.build_messages({}, [], "在吗", style_mode="model")
    check("model 模式带时效常驻", any("会变的东西" in str(m.get("content")) for m in _mf))

    # ---- 气泡数上限：长回答不能切成二十几条 ----
    _long = "这是一段很长的回答。" * 40
    _parts = prompt_fresh.split_reply(_long, 25)
    check("长回答的分段数有上限", len(_parts) <= 6, str(len(_parts)))
    check("合并没有丢内容",
          "".join(_parts).replace(" ", "") == _long.replace(" ", ""))
    check("短回答不受影响", len(prompt_fresh.split_reply("你好。再见。", 3)) == 2)

    # ---- 轻浮语气词：拿掉但不删内容 ----
    check("哈哈哈被去掉", prompt_fresh.soften_tone("不用睡，充电算吗，哈哈哈") == "不用睡，充电算吗",
          prompt_fresh.soften_tone("不用睡，充电算吗，哈哈哈"))
    check("句首哈哈被去掉",
          prompt_fresh.soften_tone("哈哈，这备注挺贴心。") == "这备注挺贴心。")
    check("笑死被去掉", "笑死" not in prompt_fresh.soften_tone("笑死，这也行"))
    check("全是语气词时给中性兜底", prompt_fresh.soften_tone("哈哈哈哈") == "嗯。")
    check("正常句子不受影响",
          prompt_fresh.soften_tone("今天有点累，早点睡吧。") == "今天有点累，早点睡吧。")

    # ---- 影子模式：只记日志、不发消息 ----
    from app_core import shadow as shadow_mod
    shadow_mod.FOLLOWUP_LOG = TMP / "shadow-followup.jsonl"
    shadow_mod.POSITIVE_LOG = TMP / "shadow-positive.jsonl"
    check("正反馈能识别", shadow_mod.positive_signal("哈哈哈哈笑死"))
    check("普通消息不算正反馈", not shadow_mod.positive_signal("今天几号"))
    shadow_mod.record_positive("笑死，这也行", "那就这样")
    _lines = (TMP / "shadow-positive.jsonl").read_text(encoding="utf-8").strip().splitlines()
    check("正例写进了 jsonl", len(_lines) == 1 and "笑死" in _lines[0], str(len(_lines)))

    # ---- 思考模式：默认开启闲聊分流（短句闲聊关思考） ----
    check("默认分流：闲聊短句关思考", core._thinking_for("今天好累", None) is False)
    _old_tc = core.thinking_casual
    core.thinking_casual = True
    check("分流模式：闲聊短句关思考", core._thinking_for("今天好累", None) is False)
    check("带问号不关", core._thinking_for("你觉得呢？", None) is None)
    check("技术词不关", core._thinking_for("这个报错怎么修", None) is None)
    check("分享图关思考", core._thinking_for("看这个", ["x.jpg"]) is False)
    check("求助图不关", core._thinking_for("这报错怎么改", ["x.jpg"]) is None)
    check("短推论关思考", core._thinking_for("你是不是又偷偷看我了", None) is False)
    check("长句不关", core._thinking_for("我" * 40, None) is None)
    check("中长句也开思考", core._thinking_for("现在还有什么和harness差不多价格好用的a", None) is None)
    check("推荐类短句也开思考", core._thinking_for("推荐个便宜点的", None) is None)
    core.thinking_casual = _old_tc

    # ---- 模型优先：style_mode=model 只给原则和例子，不注入规则块 ----
    _m_model = prompt_fresh.build_messages({}, [], "😂", style_mode="model")
    _m_rules = prompt_fresh.build_messages({}, [], "😂", style_mode="rules")
    check("model 模式带怎么回指南",
          any("【怎么回】" in str(m.get("content")) for m in _m_model))
    check("model 模式不用代码例子", not any(m.get("content") == "？" for m in _m_model))
    check("model 模式尾部不塞句数限制",
          any("【这轮】" in str(m.get("content")) for m in _m_model)
          and not any("两个逗号" in str(m.get("content")) for m in _m_model))
    check("rules 模式不带模型指南",
          not any("【怎么回】" in str(m.get("content")) for m in _m_rules))
    _m_img_model = prompt_fresh.build_messages({}, [], "看这个", images=["x"],
                                               style_mode="model")
    _img_text = _m_img_model[-1]["content"][0]["text"]
    check("model 图片提示不预判", "自己判断" in _img_text and "图在说什么" not in _img_text,
          _img_text[-50:])
    _saved_style = config_mod._cache
    config_mod._cache = {
        **config_mod.load(),
        "chat": {**(config_mod.load().get("chat") or {}), "style_mode": "model"},
    }
    check("style_mode 可切到 model", core._style_mode() == "model")
    config_mod._cache = _saved_style

    # ---- compact 注入：一个背景块 + 尾部锚点 ----
    _m_compact = prompt_fresh.build_messages(
        {}, [], "在吗", style_mode="model", context_format="compact",
        memories=[{"text": "他提过在改简历", "age_days": 3}],
        knowledge="简历第一屏\nBOSS 打招呼",
        skills=["先给结论"], lessons="- 别瞎猜", open_items="- 问他简历进度")
    _sys = [m["content"] for m in _m_compact if m["role"] == "system"]
    _bg = next((s for s in _sys if s.startswith("【背景】")), "")
    check("compact 合并成单背景块", bool(_bg), str(_sys[-2:])[:80])
    check("compact 不再分块注入",
          not any("[你记得的事]" in s or "[你的知识索引]" in s for s in _sys))
    check("compact 背景有来源标签", "[记忆]" in _bg and "[相关知识资料]" in _bg, _bg[:80])
    check("compact 有尾部锚点", any("【这轮】" in s for s in _sys))
    check("尾部锚点足够短", len(prompt_fresh.MODEL_FIRST_TAIL) < 160
          and all(len(s) < 600 for s in _sys if "【这轮】" in s))
    check("尾部锚点在背景之后", _sys.index(_bg) < next(
        i for i, s in enumerate(_sys) if "【这轮】" in s))
    check("背景有被动触发提示", "只在相关时用" in _bg, _bg[:40])
    check("compact 背景不超预算", len(_bg) <= 1300, str(len(_bg)))
    _m_legacy = prompt_fresh.build_messages(
        {}, [], "在吗", context_format="legacy",
        memories=[{"text": "他提过在改简历", "age_days": 3}])
    check("legacy 仍分块", any("[你记得的事]" in str(m.get("content"))
                                for m in _m_legacy))
    _saved_ctx = config_mod._cache
    config_mod._cache = {
        **config_mod.load(),
        "chat": {**(config_mod.load().get("chat") or {}), "context_format": "compact"},
    }
    check("context_format 可切到 compact", core._context_format() == "compact")
    config_mod._cache = _saved_ctx

    # ---- 事后硬剪结尾建议（模型改不掉，只能剪） ----
    _c1, _h1 = prompt_fresh.strip_advice_tail("写还是串行的。你可以试试 BEGIN IMMEDIATE。")
    check("结尾建议被剪掉", _h1 and "你可以试试" not in _c1, _c1)
    _c2, _h2 = prompt_fresh.strip_advice_tail("那就别干了。这会儿都快一点了，睡吧。")
    check("正常结尾不误剪", not _h2, _c2)
    _c3, _h3 = prompt_fresh.strip_advice_tail("这事儿问我不如问你自己")
    check("单句不剪", not _h3, _c3)
    _c4, _h4 = prompt_fresh.strip_advice_tail("知识有截止时间。要搜就用搜索工具？")
    check("问句结尾不剪", not _h4, _c4)
    # 事实句不能被当成建议剪掉（「可以并发读」不是劝告）
    check("事实句不算建议", not prompt_fresh.is_advice_sentence("可以并发读，写还是串行。"))
    # 他在征求意见时，建议就是答案，不剪
    check("征求意见时识别出来", prompt_fresh.wants_advice("我该不该换工作？"))
    _c5, _h5 = prompt_fresh.strip_advice_tail(
        "写还是串行。建议你先别上微服务。", allow_advice=True)
    check("征求意见时不剪建议", not _h5, _c5)
    # 没征求意见时，同样的句子照剪
    _c6, _h6 = prompt_fresh.strip_advice_tail("写还是串行。建议你先别上微服务。")
    check("没征求意见时剪掉", _h6 and "建议你" not in _c6, _c6)
    check("建议句判据", prompt_fresh.is_advice_sentence("建议你先看看文档")
          and not prompt_fresh.is_advice_sentence("你说是不是"))

    # ---- 提醒去重：同一件事换个说法不能说两条 ----
    remind_mod.init()
    # 时间必须从 now 派生：写死「今天 09:00」的话，跑测试时过了那个点
    # remind.add 会以「时间已经过去了」拒绝，用例从绿变红，CI 跟着挂。
    _t1 = (datetime.now() + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M")
    _t2 = (datetime.now() + timedelta(hours=1, minutes=10)).strftime("%Y-%m-%d %H:%M")
    _r1 = remind_mod.add("改那个主动回复不进上下文的 bug，先加日志", _t1)
    check("第一条提醒能建", "记下了" in _r1, _r1)
    _r2 = remind_mod.add("改那个 bug——主动回复没进上下文，先加日志再复现", _t2)
    check("近重复提醒被拦下", "已经有一条" in _r2, _r2)
    _r3 = remind_mod.add("买牛奶", _t1)
    check("不同的事照常建", "记下了" in _r3, _r3)

    # ---- 主动问过之后，短答复要能关掉（否则过 5 天又问一遍） ----
    def _fid_of(text: str) -> int:
        _c = followup_mod._conn()
        _r = _c.execute("SELECT id FROM followups WHERE text=?", (text,)).fetchone()
        _c.close()
        return int(_r[0]) if _r else 0

    followup_mod.init()
    followup_mod.add("把那个 bug 修了", "memory", "doc-x")
    _fids = _fid_of("把那个 bug 修了")
    followup_mod.mark_asked([_fids])
    _res = followup_mod.resolve("改完了")
    check("刚问过的短答复能关掉", _fids in (_res.get("done") or []), str(_res))
    _res2 = followup_mod.resolve("弄了一半")
    check("短答复也能标部分完成",
          (_res2.get("status") or {}).get(_fids) == "partial", str(_res2))
    check("没问过的短答复不乱归因",
          _fid_of("准备下周的分享") not in (followup_mod.resolve("改完了").get("done") or []))

    # 「已经答过了」也是关闭信号：线上回过这句，但账单一直挂着，5 天后又问一遍
    _res3 = followup_mod.resolve("已经答过了")
    check("「已经答过了」也算关闭", _fids in (_res3.get("done") or []), str(_res3))

    # 「忘了 / 算了」要关闭但不算完成，别再追问
    followup_mod.add("那件忘了的事", "memory", "doc-drop")
    _fidd = _fid_of("那件忘了的事")
    followup_mod.mark_asked([_fidd])
    _res4 = followup_mod.resolve("忘了")
    check("「忘了」归到刚问过的那条", _fidd in (_res4.get("drop") or []), str(_res4))
    followup_mod.apply(_res4)
    _con = sqlite3.connect(TMP / "chat.db")
    _st = _con.execute("SELECT status, done FROM followups WHERE id=?", (_fidd,)).fetchone()
    _con.close()
    check("「忘了」标成已取消而不是完成", _st == ("cancelled", 1), str(_st))

    # ---- 归因收窄：无关完成句不能关掉最近问过的那条；否定/部分进展要留痕 ----
    followup_mod.add("把数据库脚本跑一遍", "memory", "doc-db")
    _fid_db = _fid_of("把数据库脚本跑一遍")
    followup_mod.mark_asked([_fid_db])
    _unrel = followup_mod.resolve("我把饭做完了")
    check("无关完成句不关闭最近追问",
          _fid_db not in (_unrel.get("done") or [])
          and _fid_db not in (_unrel.get("status") or {})
          and _fid_db not in (_unrel.get("drop") or []), str(_unrel))

    _neg2 = followup_mod.resolve("还没")
    check("否定短答复绑定刚问过的那条",
          _fid_db in (_neg2.get("touched") or []) and not _neg2.get("done"),
          str(_neg2))
    followup_mod.mark_answered(_neg2["touched"])
    _c2 = sqlite3.connect(TMP / "chat.db")
    _ans = _c2.execute("SELECT answered_ts FROM followups WHERE id=?",
                       (_fid_db,)).fetchone()[0]
    _c2.close()
    check("否定短答复刷新了回应时间", float(_ans or 0) > 0, str(_ans))
    check("回应后不再进追问候选",
          _fid_db not in [x["id"] for x in followup_mod.due_for_asking(1000)],
          str([x["text"] for x in followup_mod.due_for_asking(1000)][:3]))

    _part2 = followup_mod.resolve("还在弄")
    check("部分进展绑定刚问过的那条",
          (_part2.get("status") or {}).get(_fid_db) == "partial", str(_part2))
    followup_mod.apply(_part2)
    _c2 = sqlite3.connect(TMP / "chat.db")
    _row_p = _c2.execute("SELECT status, answered_ts FROM followups WHERE id=?",
                         (_fid_db,)).fetchone()
    _c2.close()
    check("部分进展同时刷新回应时间",
          _row_p[0] == "partial" and float(_row_p[1] or 0) > 0, str(_row_p))

    # ---- 主动追问：每一条发送各自结算，重启不重计 ----
    from app_core.proactive_runtime import ProactiveRuntime

    class _PCore:
        def __init__(self, session: str = "private:proactive-test") -> None:
            self._session = session

        def private_session(self) -> str:
            return self._session

    followup_mod.add("测试主动追问结算", "memory", "doc-pro")
    _fid_p = _fid_of("测试主动追问结算")
    _pr = ProactiveRuntime(_PCore(), send=lambda t: True, db_path=TMP / "chat.db")
    _pr.init()
    _pc = sqlite3.connect(TMP / "chat.db")
    _old = time.time() - 3 * 3600
    with _pc:
        _pc.executemany(
            "INSERT INTO proactive_log(ts,topic_key,topic,text,settled_ts,ignored) "
            "VALUES(?,?,?,?,0,0)",
            [(_old, f"fu:{_fid_p}", "x", "y"),
             (_old + 1, f"fu:{_fid_p}", "x", "y")])
    _pc.close()
    _pr._settle_ignored()
    _pc = sqlite3.connect(TMP / "chat.db")
    _row_i = _pc.execute("SELECT ignored, status FROM followups WHERE id=?",
                         (_fid_p,)).fetchone()
    _pc.close()
    check("两次没回应各记一次（同一话题不合并）",
          _row_i[0] == 2 and _row_i[1] == "postponed", str(_row_i))
    _pr2 = ProactiveRuntime(_PCore(), send=lambda t: True, db_path=TMP / "chat.db")
    _pr2.init()
    _pr2._settle_ignored()
    _pc = sqlite3.connect(TMP / "chat.db")
    _row_i2 = _pc.execute("SELECT ignored FROM followups WHERE id=?",
                          (_fid_p,)).fetchone()
    _pc.close()
    check("重启后同一次发送不重计", _row_i2[0] == 2, str(_row_i2))

    # 结算与回应率同一口径：只看发送后 REPLY_WINDOW 内的回复
    def _fu_id(text: str) -> int:
        followup_mod.add(text, "memory", "doc-window")
        return _fid_of(text)

    def _add_send(fid: int, ts: float) -> None:
        _c = sqlite3.connect(TMP / "chat.db")
        with _c:
            _c.execute(
                "INSERT INTO proactive_log(ts,topic_key,topic,text,settled_ts,ignored) "
                "VALUES(?,?,?,?,0,0)", (ts, f"fu:{fid}", "x", "y"))
        _c.close()

    def _add_user(ts: float, session: str = "private:proactive-test") -> None:
        _c = sqlite3.connect(TMP / "chat.db")
        with _c:
            _c.execute("INSERT INTO messages(session,role,content,ts) VALUES(?,?,?,?)",
                       (session, "user", "嗯", ts))
        _c.close()

    def _ignored(fid: int) -> int:
        _c = sqlite3.connect(TMP / "chat.db")
        _row = _c.execute("SELECT ignored FROM followups WHERE id=?", (fid,)).fetchone()
        _c.close()
        return int(_row[0] if _row else 0)

    _now = time.time()
    # 每个用例独立会话：共享会话会让上一条用例的回复落进下一条的窗口。
    _fid_in = _fu_id("买牛奶的窗口内回复")
    _add_send(_fid_in, _now - 3 * 3600)
    with sqlite3.connect(TMP / "chat.db") as _c:
        _c.execute("UPDATE proactive_log SET text=? WHERE topic_key=?",
                   ("买牛奶了吗？", f"fu:{_fid_in}"))
        _c.executemany(
            "INSERT INTO messages(session,role,content,ts) VALUES(?,?,?,?)",
            [("private:pr-in", "assistant", "买牛奶了吗？", _now - 3 * 3600),
             ("private:pr-in", "user", "还没", _now - 2 * 3600)])
    _c.close()
    _pr3 = ProactiveRuntime(_PCore("private:pr-in"), send=lambda t: True,
                            db_path=TMP / "chat.db")
    _pr3.init()
    _pr3._settle_ignored()
    check("窗口内回复 → 不算没理", _ignored(_fid_in) == 0, str(_ignored(_fid_in)))

    _fid_out = _fu_id("体检预约的窗口外回复")
    _add_send(_fid_out, _now - 3 * 3600)
    _add_user(_now - 60, "private:pr-out")   # 发送后约 3 小时，超出 2 小时窗口
    _pr_out = ProactiveRuntime(_PCore("private:pr-out"), send=lambda t: True,
                               db_path=TMP / "chat.db")
    _pr_out.init()
    _pr_out._settle_ignored()
    check("窗口外回复 → 算没理", _ignored(_fid_out) == 1, str(_ignored(_fid_out)))

    # 停机跨多个窗口：恢复后只有窗口外的一句回复，不能把两条旧追问一起免掉
    _fid_down = _fu_id("交房租的停机跨窗口")
    _add_send(_fid_down, _now - 6 * 3600)
    _add_send(_fid_down, _now - 5 * 3600)
    _add_user(_now - 120, "private:pr-down")
    _pr_down = ProactiveRuntime(_PCore("private:pr-down"), send=lambda t: True,
                                db_path=TMP / "chat.db")
    _pr_down.init()
    _pr_down._settle_ignored()
    check("停机跨窗口：两条旧发送都记没理",
          _ignored(_fid_down) == 2, str(_ignored(_fid_down)))

    _exact_fid = _fu_id("领取新租房合同")
    _exact_q = "新的租房合同领到了吗？"
    _exact_session = "private:pr-exact"
    _exact = ProactiveRuntime(_PCore(_exact_session), send=lambda t: True,
                              db_path=TMP / "chat.db")
    _exact.init()
    with sqlite3.connect(TMP / "chat.db") as _c:
        _c.executemany(
            "INSERT INTO proactive_log(ts,topic_key,topic,text,session,platform_ref) VALUES(?,?,?,?,?,?)",
            [(_now - 4 * 3600, f"fu:{_exact_fid}", "合同", _exact_q, _exact_session, "qq:old"),
             (_now - 3 * 3600, f"fu:{_exact_fid}", "合同", _exact_q, _exact_session, "qq:new"),
             (_now - 3 * 3600, f"fu:{_exact_fid}", "合同", _exact_q, _exact_session, "feishu:new")])
        _reply_id = _c.execute(
            "INSERT INTO messages(session,role,content,ts) VALUES(?,?,?,?)",
            (_exact_session, "user", "还没。", _now - 130 * 60)).lastrowid
    _c.close()
    followup_mod.answer_turn("还没。", _exact_session, _reply_id, reply_to="qq:new")
    _exact._settle_ignored()
    _exact._settle_ignored()
    with sqlite3.connect(TMP / "chat.db") as _c:
        _outcomes = _c.execute(
            "SELECT platform_ref, ignored, answered_ts>0 FROM proactive_log "
            "WHERE session=? ORDER BY id", (_exact_session,)).fetchall()
    _c.close()
    check("重复问题按通道和平台消息 ID 只回答该次发送",
          _outcomes == [("qq:old", 1, 0), ("qq:new", 0, 1), ("feishu:new", 1, 0)],
          str(_outcomes))
    check("主动回应率与逐次结算同口径", _exact.reply_rate() == 1 / 3)

    # 没有可信主人会话：不结算、不把陌生用户消息当回应/活跃
    class _NoOwnerCore:
        def private_session(self) -> str:
            return ""

    _fid_none = _fu_id("修打印机的无主会话")
    _add_send(_fid_none, _now - 3 * 3600)
    _pr4 = ProactiveRuntime(_NoOwnerCore(), send=lambda t: True, db_path=TMP / "chat.db")
    _pr4.init()
    _pr4._settle_ignored()
    check("没有主人会话 → 不结算", _ignored(_fid_none) == 0, str(_ignored(_fid_none)))
    check("没有主人会话 → 不拿所有用户当活跃",
          _pr4.last_user_ts() == 0.0 and _pr4.reply_rate() is None,
          f"{_pr4.last_user_ts()}/{_pr4.reply_rate()}")

    _cold = prompt_fresh.style_block("嗯", [], {}, [], burst=3,
                                     recent_user_lens=[1, 1, 2])
    check("连发短句如实给平均", "最近他平均 1 字" in _cold, _cold)
    _warm = prompt_fresh.style_block("嗯", [], {}, [], burst=3,
                                     recent_user_lens=[30, 40, 25])
    check("连发长句也如实给平均", "最近他平均 32 字" in _warm, _warm)

    # ---- 长问题：给事实，不给格式 ----
    _deep = prompt_fresh.style_block(
        "为什么 SQLite 的 WAL 在多进程下容易锁，能解释下机制吗", [], {}, [])
    check("长问题给事实不给格式", "是在问你" in _deep and "先给结论" not in _deep, _deep)

    from app_core import wiki as wiki_fresh
    check("价格类保质期 14 天", wiki_fresh.ttl_days("RTX 4080 价格") == 14)
    check("版本类保质期 30 天", wiki_fresh.ttl_days("v2.1 版本发布") == 30)
    check("政策类保质期 180 天", wiki_fresh.ttl_days("新的政策规定") == 180)
    check("判不出类别按半年", wiki_fresh.ttl_days("他喜欢喝茶") == 180)
    check("计费/峰谷价也算价格类（实测漏判过）",
          wiki_fresh.ttl_days("峰谷价与缓存费") == 14)

    old = time.time() - 40 * 86400
    stale_ok, sd, st = wiki_fresh.is_stale(
        {"title": "显卡价格", "summary": "4080S 3000 元", "mtime": old})
    check("40 天前的价格知识算过时", stale_ok and sd >= 40 and st == 14,
          f"{stale_ok} {sd} {st}")
    stale_no, _, _ = wiki_fresh.is_stale(
        {"title": "他喜欢喝茶", "summary": "不加糖", "mtime": old})
    check("40 天前的生活常识不算过时", not stale_no, f"{stale_no}")

    from app_core import watch as watch_mod
    from pathlib import Path as _P
    saved_watch = watch_mod.WATCH
    watch_mod.WATCH = _P(TMP) / "watchlist.json"
    try:
        watch_mod.save([{"topic": "显卡价格", "last": "", "ts": 0}])
        items = watch_mod.load()
        check("话题表能存能读", len(items) == 1 and items[0]["topic"] == "显卡价格",
              str(items))

        async def fake_search(q, n=5):
            return f"搜索结果：\n1. {q}\n   （2026-11-03）价格 2999"

        async def fake_complete(msgs, max_tokens=None, **kw):
            return "降价了 200，现在 2999（11-03）"

        changed = await watch_mod.check_one(items[0], fake_search, fake_complete)
        check("有变化时产出变化描述", "2999" in changed, changed)
        check("变化写回内存里的 last", "2999" in items[0]["last"], items[0]["last"])

        # check_all 才负责落盘：单个 check_one 不写文件，避免中途失败留下半截状态
        watch_mod.save([{"topic": "显卡价格", "last": "", "ts": 0}])
        changed_all = await watch_mod.check_all(fake_search, fake_complete)
        check("check_all 汇总变化", len(changed_all) == 1 and "2999" in changed_all[0],
              str(changed_all))
        check("check_all 把 last 落盘", "2999" in watch_mod.load()[0]["last"],
              watch_mod.load()[0]["last"])

        async def quiet_complete(msgs, max_tokens=None):
            return "无"

        watch_mod.save([{"topic": "显卡价格", "last": "2999", "ts": 0}])
        quiet = await watch_mod.check_one(watch_mod.load()[0], fake_search,
                                          quiet_complete)
        check("没变化时保持沉默", quiet == "", repr(quiet))
    finally:
        watch_mod.WATCH = saved_watch

    # 情绪时间常数从 config 读：τ 越小忘得越快
    saved_cache = config_mod._cache
    real_mood_cfg = dict((config_mod.load().get("mood") or {}))
    try:
        config_mod._cache = {**config_mod.load(),
                             "mood": {**real_mood_cfg, "tau_valence_hours": 1.0}}
        fast_d = fresh_mood(valence=0.8)
        mood_mod.settle(fast_d, fast_d["updated_at"] + 3 * 3600)
        config_mod._cache = {**config_mod.load(),
                             "mood": {**real_mood_cfg, "tau_valence_hours": 30.0}}
        slow_d = fresh_mood(valence=0.8)
        mood_mod.settle(slow_d, slow_d["updated_at"] + 3 * 3600)
        check("情绪时间常数读配置", fast_d["valence"] < slow_d["valence"],
              f"{fast_d['valence']:.3f} vs {slow_d['valence']:.3f}")
    finally:
        config_mod._cache = saved_cache

    # 情绪影响分段长度
    check("累了分段更碎", core.seg_max({"energy": 0.2, "valence": 0.0}) == 18)
    check("有精神时句子更长", core.seg_max({"energy": 0.9, "valence": 0.5}) == 32)
    check("普通状态默认 25", core.seg_max({"energy": 0.6, "valence": 0.0}) == 25)

    # 对话原文归档：从 messages 表还原成 markdown
    con = sqlite3.connect(TMP / "chat.db")
    base_ts = time.mktime((2026, 10, 5, 9, 30, 0, 0, 0, -1))
    with con:
        con.execute("INSERT INTO messages(session, role, content, ts) VALUES (?,?,?,?)",
                    ("private:t", "user", "今天好烦", base_ts))
        con.execute("INSERT INTO messages(session, role, content, ts) VALUES (?,?,?,?)",
                    ("private:t", "assistant", "怎么了", base_ts + 60))
        # 陌生人的私聊同一天也在库里：归档/日记都不能把它当成「他说的」
        con.execute("INSERT INTO messages(session, role, content, ts) VALUES (?,?,?,?)",
                    ("private:stranger", "user", "陌生人的话不该出现", base_ts + 120))
    con.close()
    msg_raw = learn_mod.archive_raw("2026-10-05", session="private:t")
    p_raw_day = learn_mod.RAW / "2026-10-05.md"
    check("原文归档写成文件", p_raw_day.exists(), msg_raw)
    txt_raw = p_raw_day.read_text(encoding="utf-8") if p_raw_day.exists() else ""
    check("原文带时间与说话人",
          "[09:30] 他：今天好烦" in txt_raw and "[09:31] 我：怎么了" in txt_raw,
          txt_raw[:50].replace("\n", " "))
    check("原文归档不含陌生人的话", "陌生人的话不该出现" not in txt_raw,
          txt_raw[:80].replace("\n", " "))
    from app_core import diary as diary_mod
    diary_mod.CHAT_DB = TMP / "chat.db"      # 日记模块也有自己的库路径，测试要指过来
    _rows_d = diary_mod._day_messages("2026-10-05", session="private:t")
    check("日记素材只取本人会话",
          len(_rows_d) == 2 and all("陌生人" not in (c or "") for _, c, _ in _rows_d),
          str([c[:8] for _, c, _ in _rows_d]))
    check("空日子不报错", "没有对话" in learn_mod.archive_raw("2026-10-06"),
          learn_mod.archive_raw("2026-10-06"))

    # raw/ 一天几百行，只该索引最近两周，否则会把整理过的笔记挤出召回
    raw_kb = kb_mod.LEARN / "raw"
    raw_kb.mkdir(parents=True, exist_ok=True)
    fresh_raw = raw_kb / "2026-10-31.md"
    fresh_raw.write_text("# 原文\n\n[10:00] 他：今天好烦\n", encoding="utf-8")
    old_raw = raw_kb / "2026-01-01.md"
    old_raw.write_text("# 旧原文\n", encoding="utf-8")
    old_ts = time.time() - 30 * 86400
    os.utime(old_raw, (old_ts, old_ts))
    files_kb = [str(p) for p in kb_mod._files()]
    check("raw 只索引最近两周",
          str(fresh_raw) in files_kb and str(old_raw) not in files_kb,
          f"{len(files_kb)} 个文件")

    return _P, fresh_mood, prompt_fresh
