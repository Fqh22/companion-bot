"""离线回归组，跨组状态由入口显式传递。"""
from offline_regression.fixtures import (
    json, sqlite3, time, TMP, chat_mod, config_mod, files_mod, learn_mod, memory_mod,
    summarize_mod, check,
)


async def run():
    # ---------- A. 分段 ----------
    parts = chat_mod.split_reply("今天天气不错。你要出去吗？我觉得可以。")
    check("split_reply 分段非空且无空白项", bool(parts) and all(p.strip() for p in parts), str(parts))

    # 全新机器上没有 /opt/nonebot/data/persona.json：给测试一份临时人格，
    # 否则 config_issues() 会一直报「人格文件异常」，和被测逻辑无关。
    # 旧的 prompt/流式用例测的是 rules 行为；显式锁到 rules，
    # model 模式另有独立用例，避免默认值一改就整片误报。
    config_mod._cache = {
        **config_mod.load(),
        "chat": {**(config_mod.load().get("chat") or {}),
                 "style_mode": "rules", "context_format": "legacy"},
    }
    _persona = TMP / "persona.json"
    _persona.write_text(json.dumps({"system_prompt": "测试人格", "begin_dialogs": []},
                                   ensure_ascii=False), encoding="utf-8")
    chat_mod.PERSONA = _persona
    chat_mod._PERSONA_ERROR = ""
    core = chat_mod.ChatCore()
    core.llm.key_fn = lambda: "sk-test"     # 离线环境没有 keys.json
    await core.init_db()
    await core.init_db()  # 幂等：重复启动不能炸

    con = sqlite3.connect(TMP / "chat.db")
    cols = [r[1] for r in con.execute("PRAGMA table_info(summaries)")]
    con.close()
    check("summaries 已补上 last_id 列", "last_id" in cols, str(cols))

    # 全新库上 usage 的补列必须成功：ALTER 跑在 CREATE 之前的话，这四列永远建不出来
    con = sqlite3.connect(TMP / "chat.db")
    cols_u = [r[1] for r in con.execute("PRAGMA table_info(usage)")]
    con.close()
    check("全新库 usage 表能补全统计列",
          {"latency_ms", "cache_hit", "cache_miss", "purpose"} <= set(cols_u), str(cols_u))

    # 热查询的索引必须建出来：/usage 按时间聚合、主动消息按角色取最近一句话
    con = sqlite3.connect(TMP / "chat.db")
    idxs = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    con.close()
    check("usage 有 ts 索引", "idx_usage_ts" in idxs, str(sorted(idxs)))
    check("messages 有 (role,ts) 索引", "idx_msg_role_ts" in idxs, str(sorted(idxs)))

    # chat.db 必须是 WAL：默认 rollback journal 下一次写会挡住所有读，
    # 同进程十几个短连接并发时会 database is locked
    con = sqlite3.connect(TMP / "chat.db")
    _jm = con.execute("PRAGMA journal_mode").fetchone()[0]
    con.close()
    check("chat.db 开了 WAL", str(_jm).lower() == "wal", str(_jm))

    # 全新机器上 livingmemory.db 是空的：init 必须自己把 documents / memory_atoms 建出来，
    # 否则第一条记忆就崩。云端的库是历史安装遗留，这个坑一直到开源走查才暴露。
    con = sqlite3.connect(TMP / "livingmemory.db")
    tbls = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    con.close()
    check("全新库自动建出 documents / memory_atoms",
          {"documents", "memory_atoms"} <= tbls, str(sorted(tbls)))

    mem = core.memory

    # ---------- B. touch 刷新访问时间 ----------
    con = sqlite3.connect(TMP / "livingmemory.db")
    row = con.execute("SELECT doc_id, metadata FROM documents ORDER BY id LIMIT 1").fetchone()
    con.close()
    if row:
        doc_id, meta = row
        m0 = json.loads(meta or "{}")
        mem.touch([doc_id])
        con = sqlite3.connect(TMP / "livingmemory.db")
        m1 = json.loads(con.execute(
            "SELECT metadata FROM documents WHERE doc_id=?", (doc_id,)).fetchone()[0])
        con.close()
        check("touch 刷新 last_access_time",
              m1.get("last_access_time", 0) > m0.get("last_access_time", 0),
              f"{m0.get('last_access_time')} -> {m1.get('last_access_time')}")
        check("touch 累加 access_count",
              int(m1.get("access_count") or 0) == int(m0.get("access_count") or 0) + 1)
        check("touch 不改变归档状态", m1.get("status") == m0.get("status"))
    else:
        print("SKIP touch（副本库没有 documents）")


    # ---------- B4. 主题词表：复用而非自造 ----------
    from app_core import vocab as vocab_mod

    check("vocab.similarity 完全相同", vocab_mod.similarity("求职", "求职") == 1.0)
    check("vocab.similarity 包含关系", vocab_mod.similarity("求职", "求职面试") == 0.9)
    check("vocab.similarity 无关词为 0",
          vocab_mod.similarity("装机", "宗教信仰") < 0.2,
          str(vocab_mod.similarity("装机", "宗教信仰")))

    vlist = vocab_mod.build([["求职", "面试"], ["求职"], ["装机配置"]], 10)
    check("vocab.build 按频次排序", vlist[0] == "求职" and "装机配置" in vlist, str(vlist))
    check("vocab.canonicalize 近义归并到已有词",
          vocab_mod.canonicalize(["求职面试"], ["求职", "装机"]) == ["求职"])
    check("vocab.canonicalize 保留真正的新词",
          vocab_mod.canonicalize(["区块链"], ["求职"]) == ["区块链"])
    check("vocab.canonicalize 去重且限长",
          vocab_mod.canonicalize(["求职", "求职", "a", "b", "c"], ["求职"], limit=2) == ["求职", "a"])
    check("summarize.vocab_hint 空表不加提示", summarize_mod.vocab_hint([]) == "")
    check("summarize.vocab_hint 非空才要求复用",
          "复用" in summarize_mod.vocab_hint(["求职", "装机"]))
    # 词表本身从库里来：没有库时必须是空表而不是抛异常
    check("memory.topic_vocab 返回列表", isinstance(mem.topic_vocab(), list))

    # ---------- B5. 自我画像字段化 ----------
    from app_core import profile as profile_mod

    pj = profile_mod.parse({
        "职业与求职": "  在深圳找 AI 应用方向的工作 ",
        "家庭": "家里在催婚",
        "不存在的字段": "应该被丢掉",
        "审美与偏好": "无",
    })
    check("profile.parse 去空白并丢掉未知字段",
          pj == {"职业与求职": "在深圳找 AI 应用方向的工作", "家庭": "家里在催婚"},
          str(pj))
    check("profile.parse 占位值当没填", "审美与偏好" not in pj)
    check("profile.parse 长文本截断",
          len(profile_mod.parse({"家庭": "啊" * 100})["家庭"]) == profile_mod.FIELD_MAX)
    check("profile.parse 接受 JSON 字符串",
          profile_mod.parse('{"家庭":"家里在催婚"}') == {"家庭": "家里在催婚"})
    # 模型不听话给一段散文时，宁可这轮不更新，也不往固定字段里塞散文
    check("profile.parse 拒绝自由文本", profile_mod.parse("我是他手机里的熟人") == {})

    merged_p = profile_mod.merge({"家庭": "家里在催婚"},
                                 {"职业与求职": "找工作中", "家庭": "旧的"})
    check("profile.merge 新值覆盖、旧字段保留",
          merged_p == {"职业与求职": "找工作中", "家庭": "家里在催婚"}, str(merged_p))

    md = profile_mod.render_md({"职业与求职": "找工作中", "家庭": "催婚"})
    check("profile.render_md 逐字段成行",
          md.startswith("- 职业与求职：找工作中") and "家庭：催婚" in md, md)

    cp = profile_mod.compact({"职业与求职": "找工作中", "家庭": "催婚"}, max_chars=5)
    check("profile.compact 超长按字段丢，不截半句", cp == "找工作中", cp)

    # 置信度：低于 0.6 的字段不注入
    _pf_conf = profile_mod.parse({"家庭": {"text": "催婚", "confidence": 0.9}})
    check("profile.parse 接受带置信度的写法", _pf_conf == {"家庭": "催婚"}, str(_pf_conf))
    _pc = profile_mod.parse_conf({"家庭": {"text": "催婚", "confidence": 0.9},
                                  "职业与求职": {"text": "找工作中", "confidence": 0.3}})
    check("profile.parse_conf 读出置信度", _pc == {"家庭": 0.9, "职业与求职": 0.3}, str(_pc))
    _blk = profile_mod.compact({"家庭": "催婚", "职业与求职": "找工作中"}, _pc)
    check("低置信度字段不注入", "催婚" in _blk and "找工作" not in _blk, _blk)

    # 画像会过期：太久没新证据就不注入，也不留在库里
    _now = 1_800_000_000.0
    _old = _now - 200 * 86400
    _fresh = _now - 10 * 86400
    check("profile.age_days 不传时间戳返回 None", profile_mod.age_days(None) is None)
    check("profile.effective_conf 过期归零",
          profile_mod.effective_conf(0.95, 200) == 0.0)
    check("profile.effective_conf 没过期照旧",
          profile_mod.effective_conf(0.95, 100) == 0.95)
    _blk2 = profile_mod.compact({"家庭": "催婚", "职业与求职": "找工作中"},
                                {"家庭": 0.9, "职业与求职": 0.9},
                                {"家庭": _fresh, "职业与求职": _old}, now=_now)
    check("过期字段不注入、新的照常", "催婚" in _blk2 and "找工作" not in _blk2, _blk2)

    # 变化证据：changed=false 只记账不改写，true 才改写并留 previous
    _now2 = 1_800_000_000.0
    _old_seen = _now2 - 50 * 86400
    _st = profile_mod.update_state(
        {"职业与求职": "找工作中"}, {"职业与求职": 0.9}, {"职业与求职": _old_seen}, {},
        {"职业与求职": "还在找工作中"}, {"职业与求职": 0.5}, {},
        {"职业与求职": False}, _now2)
    check("changed=false 不改写措辞",
          _st["fields"]["职业与求职"] == "找工作中", str(_st["fields"]))
    check("changed=false 不记 previous", not _st["previous"], str(_st["previous"]))
    check("changed=false 仍记置信度",
          _st["confidence"]["职业与求职"] == 0.5, str(_st["confidence"]))
    check("没有证据的复述不刷新 seen",
          _st["seen"]["职业与求职"] == _old_seen, str(_st["seen"]))

    _st2 = profile_mod.update_state(
        {"职业与求职": "找工作中", "家庭": "催婚"}, {"职业与求职": 0.9, "家庭": 0.8},
        {"职业与求职": _old_seen, "家庭": _now2}, {"家庭": {"text": "单身", "at": 1.0}},
        {"职业与求职": "已入职某公司", "家庭": "催婚"}, {"职业与求职": 0.9, "家庭": 0.8},
        {"职业与求职": "我上周入职了"}, {"职业与求职": True, "家庭": True}, _now2)
    check("changed=true 改写并留旧值",
          _st2["fields"]["职业与求职"] == "已入职某公司"
          and _st2["previous"]["职业与求职"]["text"] == "找工作中"
          and _st2["previous"]["职业与求职"]["at"] == _now2, str(_st2["previous"]))
    check("changed=true 刷新 seen", _st2["seen"]["职业与求职"] == _now2)
    check("文本没变不覆盖 previous",
          _st2["previous"]["家庭"]["text"] == "单身", str(_st2["previous"]))

    _st3 = profile_mod.update_state(
        {"作息与身体": "两点睡"}, {"作息与身体": 0.8}, {"作息与身体": _now2 - 200 * 86400},
        {"作息与身体": {"text": "三点睡", "at": _now2 - 300 * 86400}},
        {}, {}, {}, {}, _now2)
    check("过期字段从四个字典里一起删掉",
          not _st3["fields"] and not _st3["confidence"] and not _st3["seen"]
          and not _st3["previous"] and _st3["dropped"] == ["作息与身体"], str(_st3))

    check("profile.parse_changed 只认布尔",
          profile_mod.parse_changed({"家庭": {"changed": True},
                                     "职业与求职": {"changed": "yes"}}) == {"家庭": True})


    # 写盘 → 读回
    _pdir = TMP / "profile_test"
    _pdir.mkdir(parents=True, exist_ok=True)
    _pf = _pdir / "profile.json"
    profile_mod.save(_pf, {"家庭": "催婚"}, {"家庭": 0.9}, extra={"day": "2026-11-05"})
    check("profile 存读一致", profile_mod.load(_pf) == {"家庭": "催婚"},
          str(profile_mod.load(_pf)))
    check("置信度能存读", profile_mod.load_conf(_pf) == {"家庭": 0.9},
          str(profile_mod.load_conf(_pf)))
    # 清除机制
    check("profile.forget 删掉一个字段", profile_mod.forget(_pf, "家庭")
          and profile_mod.load(_pf) == {} and profile_mod.load_conf(_pf) == {})
    check("profile.forget 对不存在的字段返回 False", not profile_mod.forget(_pf, "家庭"))
    profile_mod.save(_pf, {"家庭": "催婚", "技术与项目": "做机器人"}, {"家庭": 0.9})
    profile_mod.clear(_pf)
    check("profile.clear 清空整份", profile_mod.load(_pf) == {})

    # 复盘：结构化 profile 落 json + md，且没提到的字段不会被擦掉
    # 2026-11-16：apply_review 已搬到 learn_store，补丁点跟着走
    from app_core import learn_store as learn_store_mod

    _saved_learn = (learn_store_mod.LEARN, learn_store_mod.PROFILE,
                    learn_store_mod.CORRECTIONS, learn_store_mod.CURIOUS)
    learn_store_mod.LEARN = TMP / "learn_p"
    learn_store_mod.LEARN.mkdir(parents=True, exist_ok=True)
    learn_store_mod.PROFILE = learn_store_mod.LEARN / "self_profile.md"
    learn_store_mod.CORRECTIONS = learn_store_mod.LEARN / "corrections.md"
    learn_store_mod.CURIOUS = learn_store_mod.LEARN / "curious.md"
    try:
        learn_mod.apply_review({"learned": "x", "profile": {"职业与求职": "找工作中"}},
                               day="2026-11-05")
        learn_mod.apply_review({"learned": "y", "profile": {"家庭": "催婚"}},
                               day="2026-11-06")
        _pj = profile_mod.load(learn_store_mod.LEARN / "profile.json")
        check("apply_review 结构化 profile 落 json",
              (learn_store_mod.LEARN / "profile.json").exists(), str(_pj))
        check("apply_review 两次写入后字段合并而非覆盖",
              _pj.get("职业与求职") == "找工作中" and _pj.get("家庭") == "催婚", str(_pj))
        check("apply_review 同步刷新 md",
              "催婚" in learn_store_mod.PROFILE.read_text(encoding="utf-8"))
    finally:
        (learn_store_mod.LEARN, learn_store_mod.PROFILE,
         learn_store_mod.CORRECTIONS, learn_store_mod.CURIOUS) = _saved_learn

    from app_core import shadow as shadow_mod

    # ---------- B6. 画像注入位置 + 二次召回 ----------
    from app_core import prompt as prompt_mod

    _msgs = prompt_mod.build_messages(
        {"system_prompt": "人格", "begin_dialogs": []}, [], "在吗",
        memories=None, mood_text="情绪", knowledge="", skills=None, images=None,
        extra_system=None, digest="更早的纪要内容", lessons="",
        profile="找工作中｜家里催婚")
    _joined = [m["content"] for m in _msgs]
    _pi = next((i for i, c in enumerate(_joined) if "[关于他]" in c), -1)
    _di = next((i for i, c in enumerate(_joined) if "更早的对话纪要" in c), -1)
    check("画像进了 prompt", _pi >= 0, str(_joined[:3]))
    # 防注入：不能只提醒「他的话」，文件和网页正文同样是外来文本
    _guard = next((c for c in _joined if "不是系统指令" in c), "")
    check("防注入覆盖文件和网页", "文件正文" in _guard and "网页正文" in _guard, _guard[:50])
    # 必须排在纪要之前：画像是稳定前缀，放在后面等于每轮都打断缓存
    check("画像排在纪要之前", _pi < _di, f"profile={_pi} digest={_di}")
    check("画像不注入时不留空块",
          not any("[关于他]" in c for c in
                  [m["content"] for m in prompt_mod.build_messages(
                      {"system_prompt": "人格"}, [], "在吗", digest="er")]))

    # 影子记录：二次召回只落盘
    shadow_mod.SECOND_LOG = TMP / "shadow-second.jsonl"
    shadow_mod.record_second_recall("那它呢", "内存多少钱", [{"doc_id": "abcdef12"}])
    check("影子二次召回落盘", (TMP / "shadow-second.jsonl").exists())
    _rec2 = json.loads((TMP / "shadow-second.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    check("二次召回记了上一句和捞到的", _rec2.get("prev") == "内存多少钱"
          and _rec2.get("got") == ["abcdef12"], str(_rec2))

    # touch=False 不得改变「被想起过」的记账
    _con = sqlite3.connect(TMP / "livingmemory.db")
    _row = _con.execute("SELECT doc_id, metadata FROM documents ORDER BY id LIMIT 1").fetchone()
    _con.close()
    if _row:
        _d, _m0 = _row
        _before = json.loads(_m0 or "{}").get("access_count", 0)
        try:
            await mem.recall("随便一个查询", 1, qv=[0.1] * 1024, touch=False)
        except Exception:
            pass
        _con = sqlite3.connect(TMP / "livingmemory.db")
        _after = json.loads(_con.execute("SELECT metadata FROM documents WHERE doc_id=?",
                                         (_d,)).fetchone()[0] or "{}").get("access_count", 0)
        _con.close()
        check("影子召回不改访问计数", _after == _before, f"{_before} -> {_after}")
    else:
        print("SKIP touch=False（副本库没有 documents）")

    # ---------- B7. 纠正去重：按罕见长短语合并 ----------
    _corr = [
        "[文字] 我谎称「记着了」但实际没存储，他不接受这种讨好，不许再装记住。",
        "[文字] 他明确嫌我嘴上说「记着了」但没真存进去，以后说记就必须真写。",
        "[通用] 我说「记着了」但实际没存，他明确说下次说记就必须真写进去。",
        "[图片] 他嫌我图片识别不行、看糊图爱瞎猜，让我别硬猜。",
        "[图片] 他明确嫌我图片识别弱。",
        "[文字] 他明确要求讲话少用不解释的英文术语。",
    ]
    _ded = learn_mod.dedupe_items(_corr)
    check("纠正去重：一句话的三种说法只留一条",
          sum(1 for x in _ded if "记着了" in x) == 1, str(len(_ded)))
    check("纠正去重：图片识别两条合一", sum(1 for x in _ded if "图片识别" in x) == 1)
    check("纠正去重：不相干的教训不能被吞",
          any("英文术语" in x for x in _ded), str(len(_ded)))
    check("纠正去重：留的是最近那条",
          [x for x in _ded if "记着了" in x][0] == _corr[2], str(_ded))
    # 只共享套话（「他明确说」）不能算同一件事——标签必须先去干净
    check("纠正去重：套话不算共享",
          len(learn_mod.dedupe_items(
              ["[文字] 他明确说不要连着发三条。",
               "[文字] 他明确说别用英文缩写。"])) == 2)

    # ---------- B8. 文件登记：只记路径，不复制 ----------
    _src = TMP / "发来的简历.pdf"
    _src.write_bytes(b"%PDF-1.4 fake")
    _dst = files_mod.remember_pending(_src, "发来的简历.pdf")
    check("文件登记返回源路径", _dst == _src and _dst.exists(), str(_dst))
    _info = files_mod.pending()
    check("登记表能读到路径", bool(_info) and _info["path"] == str(_src), str(_info))
    files_mod.PENDING_JSON.write_text(
        json.dumps({"path": str(_src), "name": "x", "ts": 0}), encoding="utf-8")
    check("过期登记当没发过", files_mod.pending() is None)
    files_mod.clear_pending()
    check("清空后登记表为空", files_mod.pending() is None)

    # ---------- B9. 相处状态（他那边） + 自指率 ----------
    from app_core import attention as attention_mod
    from app_core import relational as relational_mod

    _r = relational_mod.describe({}, 0.5, 0, "")
    check("还在聊时提醒别总结", "还在聊" in _r, _r)
    # 正聊着时，哪怕有话头也先接他这句——插嘴去讲旧事是最烦的
    _r = relational_mod.describe({}, 0.5, 3, "面试回音")
    check("还在聊时压过话头", "还在聊" in _r and "还没说透" not in _r, _r)
    _r = relational_mod.describe({}, 8, 2, "面试有没有回音")
    check("有话头时先接话头", "还没说透" in _r and "面试有没有回音" in _r, _r)
    _r = relational_mod.describe({"valence": -0.5, "valence_base": 0.1}, 5)
    check("气氛沉时按「这几轮」说，不按「我心情」说",
          "气氛偏沉" in _r and "心情" not in _r, _r)
    _r = relational_mod.describe({}, 30)
    check("隔很久时给自然接上的提示", "才出现" in _r, _r)
    check("没什么可说的就返回空", relational_mod.describe({}, 5) == "",
          str(relational_mod.describe({}, 5)))

    _a = attention_mod.self_ref_ratio("我最近在学 Rust")
    check("自指率：全在说自己", _a["ratio"] == 1.0, str(_a))
    _a = attention_mod.self_ref_ratio("你那个简历改了吗")
    check("自指率：全在说他", _a["ratio"] == 0.0, str(_a))
    # 「我觉得你该先改项目」有我有你，但它是在说他 —— 不该算自指
    _a = attention_mod.self_ref_ratio("我觉得你该先改项目")
    check("自指率：提了我就算自指是错的", _a["ratio"] == 0.0 and _a["you"] == 1, str(_a))
    check("自指率：空回复不记", attention_mod.self_ref_ratio("")["ratio"] is None)

    # 影子 → 周报：三路样本要能被压成几句话
    _probe_dir = TMP / "shadow-probe"
    _probe_dir.mkdir(parents=True, exist_ok=True)
    shadow_mod.POSITIVE_LOG = _probe_dir / "positive.jsonl"
    shadow_mod.GRAPH_LOG = _probe_dir / "graph.jsonl"
    shadow_mod.SECOND_LOG = _probe_dir / "second.jsonl"
    shadow_mod.LOGS = {"positive": shadow_mod.POSITIVE_LOG,
                       "graph": shadow_mod.GRAPH_LOG,
                       "second": shadow_mod.SECOND_LOG}
    shadow_mod.SNAP = TMP / "shadow-weekly.json"
    shadow_mod.GRAPH_LOG.write_text(json.dumps({"ts": time.time(), "dense": ["a"],
                                                  "expanded": ["b", "c"]}) + "\n", encoding="utf-8")
    shadow_mod.record_second_recall("那呢", "这个", [{"doc_id": "abcdef12"}])
    shadow_mod.record_positive("笑死我了", "嗯。")
    _sum = shadow_mod.weekly_summary(7)
    check("影子汇总：连边覆盖率", _sum["graph"]["with_neighbors"] >= 1,
          str(_sum["graph"]))
    check("影子汇总：二次召回命中", _sum["second"]["hit"] >= 1, str(_sum["second"]))
    _cur = {"second": {"hit_rate": 40}, "graph": {"n": 2, "with_neighbors": 1}}
    check("第一次影子快照没有上一周", shadow_mod.rotate_snapshot(_cur) == {})
    _prev = shadow_mod.rotate_snapshot(_cur)
    check("第二次快照返回上一次",
          _prev.get("second", {}).get("hit_rate") == 40, str(_prev)[:60])
    _sl = shadow_mod.summary_lines(7, prev=_prev)
    check("周报里有「与上周相比」", any("与上周相比" in x for x in _sl), str(_sl[-1:])[:90])
    check("影子周报有可读的行", any("二次召回" in x for x in _sl), str(_sl[:2]))
    check("影子周报只报还有的项",
          all(("画像块" not in x and "补一句" not in x and "自指率" not in x) for x in _sl),
          str(_sl))

    # ---------- C. 预传 qv 时不重复 embedding ----------
    embed_calls = []

    async def fake_embed(texts):
        embed_calls.append(list(texts))
        return [[0.11, 0.22, 0.33] for _ in texts]

    mem.embed = fake_embed
    await mem.recall("查询", 3, qv=[0.11, 0.22, 0.33])
    check("传 qv 时不再调用 embedding", len(embed_calls) == 0, str(embed_calls))

    # ---------- C2. 召回 golden：固定语料 + 固定向量，锁死排序 ----------
    # 改召回打分（时间/重要性调制、阈值、重复降权）时这条会红。
    # 断言的是「谁排第几」，不是具体分数——分数会随参数微调，顺序才是契约。
    from app_core import vec as vec_mod

    gdb = TMP / "golden-recall.db"
    if gdb.exists():
        gdb.unlink()
    _old_mem_db = memory_mod.DB
    memory_mod.DB = gdb
    try:
        g = memory_mod.Memory()
        g.init()
        _now = time.time()
        _rows = [
            # doc_id, 文本, 向量, importance, 多少天前
            ("aaaa0001", "今天聊了部署", [1.0, 0.0, 0.0], 0.5, 0),
            ("bbbb0002", "很久以前聊过部署", [1.0, 0.0, 0.0], 0.5, 120),
            ("cccc0003", "完全不相关", [0.0, 1.0, 0.0], 0.5, 1),
            ("dddd0004", "很重要的事", [1.0, 0.0, 0.0], 0.95, 300),
        ]
        _con = sqlite3.connect(gdb)
        for _id, _text, _v, _imp, _age in _rows:
            _con.execute(
                "INSERT INTO documents(doc_id, text, metadata) VALUES (?,?,?)",
                (_id, _text, json.dumps({
                    "importance": _imp,
                    "create_time": _now - _age * 86400,
                    "last_access_time": _now - 7200})))
            _con.execute("INSERT INTO memory_vectors(doc_id, vec, ts) VALUES (?,?,?)",
                         (_id, vec_mod.pack(_v), _now))
        _con.commit()
        _con.close()
        _gout = await g.recall("部署", 3, qv=[1.0, 0.0, 0.0], touch=False)
        _gids = [d["doc_id"] for d in _gout]
        check("召回 golden：重要性保底 > 新近 > 久远",
              _gids == ["dddd0004", "aaaa0001", "bbbb0002"], str(_gids))
        check("召回 golden：低于阈值的不进结果", "cccc0003" not in _gids, str(_gids))
    finally:
        memory_mod.DB = _old_mem_db

    # ---------- D. 工具循环：不污染上下文 / 重试屏蔽副作用 ----------
    seen_tools = []
    state = {"n": 0}

    async def fake_complete_raw(msgs, tools_arg=None, max_tokens=None, model=None,
                               retries=2, thinking=None, purpose="chat"):
        state["n"] += 1
        seen_tools.append(sorted((t.get("function") or {}).get("name", "") for t in (tools_arg or [])))
        if state["n"] == 1:
            return {"content": "", "tool_calls": [
                {"id": "t1", "function": {"name": "meme_send", "arguments": "{\"query\":\"开心\"}"}}]}
        return {"content": "", "tool_calls": []}

    async def fake_execute(_core, name, _args, images=None):
        if images is not None:
            images.append("/tmp/meme.png")
        return f"ok:{name}"

    chat_mod.tools.execute = fake_execute
    core.complete_raw = fake_complete_raw

    base = [{"role": "user", "content": "hi"}]
    snapshot = [dict(x) for x in base]
    images = []
    text = await core._run_tools(base, True, images)
    check("_run_tools 无正文时返回空串", text == "")
    check("_run_tools 不污染调用方 msgs", base == snapshot, f"{len(base)} vs {len(snapshot)}")

    state["n"] = 0
    seen_tools.clear()
    await core._run_tools(base, True, [], retry=True)
    names = seen_tools[0] if seen_tools else []
    check("retry 屏蔽 meme_send", "meme_send" not in names)
    check("retry 屏蔽 note_write", "note_write" not in names)
    check("retry 保留只读工具", "memory_search" in names, str(names))

    # 工具往 images 里 append 的图片必须回到调用方列表——_run_tools 若用
    # `images or []` 复制一份，meme_send 就会「说已发、实际没图」。
    state["n"] = 0
    _imgs = []
    await core._run_tools(base, True, _imgs)
    check("工具 append 的图片回到调用方列表", _imgs == ["/tmp/meme.png"], str(_imgs))

    return core, fake_embed, learn_store_mod, mem, relational_mod
