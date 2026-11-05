"""云端 QQ 机器人 · 离线回归测试

原则：不联网（模型/embedding 全 mock）、不写生产库（库先备份到临时目录）、不发消息。
测的是 `/opt/nonebot` 下的**生产代码**，所以部署前后都能跑。

用法：
    /opt/nonebot/venv/bin/python /opt/nonebot/ops/offline-test.py

覆盖：分段、库迁移、记忆访问时间刷新、embedding 复用、工具循环副作用隔离、
      记忆总结水位、reply 全链路、模型故障降级、情绪阈值、推理截断扩容、
      配置自检、启动补偿总结、表情包分档阈值、归档恢复、影子模式去重。
"""
import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

CODE_DIR = os.environ.get("QQBOT_CODE_DIR", "/opt/nonebot")
sys.path.insert(0, CODE_DIR)

TMP = Path(tempfile.mkdtemp(prefix="qqbot-test-"))
print("代码目录:", CODE_DIR)
print("临时目录:", TMP)

# 允许本地用一份空库/schema 造出来的库跑（云端默认就是生产库，只读备份）
SRC_MEM = Path(os.environ.get("QQBOT_MEM_SRC", "/opt/qqbot-data/livingmemory.db"))
SRC_CHAT = Path(os.environ.get("QQBOT_CHAT_SRC", "/opt/nonebot/data/chat.db"))


def copy_db(src: Path, dst: Path) -> None:
    if not src.exists():
        dst.touch()
        return
    s = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    d = sqlite3.connect(dst)
    with d:
        s.backup(d)
    s.close()
    d.close()


copy_db(SRC_MEM, TMP / "livingmemory.db")
copy_db(SRC_CHAT, TMP / "chat.db")

from app_core import chat as chat_mod          # noqa: E402
from app_core import config as config_mod      # noqa: E402
from app_core import files as files_mod        # noqa: E402
from app_core import followup as followup_mod  # noqa: E402
from app_core import kb as kb_mod              # noqa: E402
from app_core import learn as learn_mod        # noqa: E402
from app_core import memory as memory_mod      # noqa: E402
from app_core import retrieval as retrieval_mod  # noqa: E402

from app_core import mood as mood_mod          # noqa: E402
from app_core import remind as remind_mod      # noqa: E402
from app_core import summarize as summarize_mod  # noqa: E402
from app_core import tools as tools_module    # noqa: E402

# 下面多处会把 tools.execute 换成桩；跑完要还原，否则同进程里后面的检查
# （以及 smoke-all.py）看到的都是假实现，真实现的问题永远测不出来。
REAL_TOOLS_EXECUTE = chat_mod.tools.execute

# 所有落盘路径都改到临时目录，确保不碰生产
chat_mod.DB = TMP / "chat.db"
chat_mod.DATA = TMP           # 只在 init_db 里用来建目录，Windows 上跑测试也不会碰 /opt
memory_mod.DB = TMP / "livingmemory.db"
kb_mod.DB = TMP / "chat.db"
mood_mod.STATE_DIR = TMP / "mood"
mood_mod.STATE = TMP / "mood" / "state.json"
mood_mod.CHAT_DB = TMP / "chat.db"
learn_mod.RAW = TMP / "raw"
learn_mod.CHAT_DB = TMP / "chat.db"
remind_mod.DB = TMP / "chat.db"
followup_mod.DB = TMP / "chat.db"
files_mod.SAVE_DIR = TMP
files_mod.IMAGE_DIR = TMP / "images"
files_mod.PENDING_JSON = TMP / "last-file.json"
files_mod.AGENT_INBOX = TMP / "agent-inbox"
# 后面的 reply 测试会把 kb.recall 换成桩，这里先留一份真的给知识库用例用
REAL_KB_RECALL = kb_mod.recall

RESULTS = []


def check(name: str, cond: bool, extra: str = "") -> None:
    RESULTS.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + name + ((" | " + extra) if extra else ""))


async def main() -> None:
    # ---------- A. 分段 ----------
    parts = chat_mod.split_reply("今天天气不错。你要出去吗？我觉得可以。")
    check("split_reply 分段非空且无空白项", bool(parts) and all(p.strip() for p in parts), str(parts))

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

    # ---------- B2. 关键词检索：BM25 + RRF（纯函数，不碰库） ----------
    check("tokenize 中文切 bigram",
          retrieval_mod.tokenize("今天天气") == ["今天", "天天", "天气"],
          str(retrieval_mod.tokenize("今天天气")))
    check("tokenize ASCII 整体保留、小写",
          retrieval_mod.tokenize("DeepSeek") == ["deepseek"],
          str(retrieval_mod.tokenize("DeepSeek")))
    check("tokenize 单字保留", retrieval_mod.tokenize("好") == ["好"])
    # 数字 ID 被 bigram 切碎就废了：它恰恰是 dense 通道最抓不住、最需要词面命中的东西
    check("tokenize 数字 ID 不切碎",
          retrieval_mod.tokenize("QQ 10000") == ["qq", "10000"],
          str(retrieval_mod.tokenize("QQ 10000")))

    docs = [
        "我下周三要去杭州出差，订了 10 点的票",
        "他说最近在学 Rust，想写个命令行工具",
        "QQ 号 10000 是机器人的",
        "今天天气不错",
    ]
    bm = retrieval_mod.BM25(docs)
    check("BM25 数字 ID 命中排第一", bm.ranked("10000")[:1] == [2],
          str(bm.ranked("10000")))
    check("BM25 中文短 query 命中", bm.ranked("天气")[:1] == [3], str(bm.ranked("天气")))
    check("BM25 无命中返回空", bm.ranked("区块链") == [])
    check("BM25 空 query 不炸",
          bm.ranked("") == [] and bm.scores("") == [0.0] * len(docs))
    check("BM25 空库不炸", retrieval_mod.BM25([]).ranked("任意") == [])

    fused = retrieval_mod.rrf([["a", "b"], ["a", "c"]])
    check("RRF 双路第一胜出", max(fused, key=fused.get) == "a", str(fused))
    check("RRF 名次分 = 1/(k+rank)",
          abs(fused["a"] - 2 / 61) < 1e-9 and abs(fused["b"] - 1 / 62) < 1e-9,
          str(fused))
    check("fuse 单路为空时退化",
          retrieval_mod.fuse(["a", "b"], [], limit=5) == ["a", "b"]
          and retrieval_mod.fuse([], ["c"], limit=5) == ["c"])
    # d1：dense 第 1 + 关键词第 2 → 1/61+1/62；d3 只在关键词第 1 → 1/61。d1 该赢
    check("fuse 交叉命中排前", retrieval_mod.fuse(["d1", "d2"], ["d3", "d1"], limit=3)[0] == "d1")

    # 集成：影子对比真跑一遍，确认只落盘、不抛、且不改召回结果
    from app_core import shadow as shadow_mod

    shadow_path = TMP / "shadow-retrieval.jsonl"
    shadow_mod.RETRIEVAL_LOG = shadow_path
    bm_idx, bm_ids = mem._bm25_index()
    if bm_idx is not None and bm_ids:
        before = shadow_path.stat().st_size if shadow_path.exists() else 0
        mem._shadow_retrieval("测试", bm_ids[:5], bm_ids[:3], 3)
        check("影子召回对比落盘", shadow_path.exists() and shadow_path.stat().st_size > before)
        rec = json.loads(shadow_path.read_text(encoding="utf-8").splitlines()[-1])
        check("影子记录有三路名单 + overlap",
              {"dense", "bm25", "fused", "overlap"} <= set(rec), str(sorted(rec)))
    else:
        print("SKIP 影子召回对比（副本库没有向量）")

    # ---------- B3. 实体连边：共同实体扩展 ----------
    from app_core import memgraph as memgraph_mod

    check("memgraph.normalize 去空白/统一小写",
          memgraph_mod.normalize("Harness 架构") == memgraph_mod.normalize("harness架构"),
          memgraph_mod.normalize("Harness 架构"))

    rows = [
        (1, ["装机", "内存"]),
        (2, ["装机", "显卡"]),
        (3, ["显卡"]),
        (4, ["只出现一次"]),
    ]
    g = memgraph_mod.EntityGraph(rows)
    check("memgraph 统计（df=1 的实体被剪掉）",
          g.stats() == {"docs": 4, "entities": 4, "usable": 2}, str(g.stats()))
    check("memgraph 共同实体连到邻居", [p for p, _ in g.related([1])] == [2],
          str(g.related([1])))
    check("memgraph 邻居排除种子自己",
          1 not in [p for p, _ in g.related([1])] and 2 not in [p for p, _ in g.related([2])])
    check("memgraph 多跳排序稳定", [p for p, _ in g.related([2])] == [1, 3],
          str(g.related([2])))
    check("memgraph 空图不炸", memgraph_mod.EntityGraph([]).related([1]) == [])

    # 大标签要剪掉：10 篇都带「热门」时不该把整团拉进来
    hub = memgraph_mod.EntityGraph([(i, ["热门"]) for i in range(10)])
    check("memgraph 高频标签被当停用词", hub.related([0]) == [], str(hub.stats()))

    graph_path = TMP / "shadow-graph.jsonl"
    shadow_mod.GRAPH_LOG = graph_path
    shadow_mod.record_graph("装机配置怎么选", dense=["aaaa1111"],
                            expanded=["bbbb2222", "aaaa1111"])
    rec_g = json.loads(graph_path.read_text(encoding="utf-8").splitlines()[-1])
    check("影子连边落盘且只记新增", rec_g.get("expanded") == ["bbbb2222"], str(rec_g))

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
    _saved_learn = (learn_mod.LEARN, learn_mod.PROFILE, learn_mod.CORRECTIONS,
                    learn_mod.CURIOUS)
    learn_mod.LEARN = TMP / "learn_p"
    learn_mod.LEARN.mkdir(parents=True, exist_ok=True)
    learn_mod.PROFILE = learn_mod.LEARN / "self_profile.md"
    learn_mod.CORRECTIONS = learn_mod.LEARN / "corrections.md"
    learn_mod.CURIOUS = learn_mod.LEARN / "curious.md"
    try:
        learn_mod.apply_review({"learned": "x", "profile": {"职业与求职": "找工作中"}},
                               day="2026-11-05")
        learn_mod.apply_review({"learned": "y", "profile": {"家庭": "催婚"}},
                               day="2026-11-06")
        _pj = profile_mod.load(learn_mod.LEARN / "profile.json")
        check("apply_review 结构化 profile 落 json",
              (learn_mod.LEARN / "profile.json").exists(), str(_pj))
        check("apply_review 两次写入后字段合并而非覆盖",
              _pj.get("职业与求职") == "找工作中" and _pj.get("家庭") == "催婚", str(_pj))
        check("apply_review 同步刷新 md",
              "催婚" in learn_mod.PROFILE.read_text(encoding="utf-8"))
    finally:
        (learn_mod.LEARN, learn_mod.PROFILE, learn_mod.CORRECTIONS,
         learn_mod.CURIOUS) = _saved_learn

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

    # 影子记录：画像与二次召回都只落盘
    shadow_mod.PROFILE_LOG = TMP / "shadow-profile.jsonl"
    shadow_mod.SECOND_LOG = TMP / "shadow-second.jsonl"
    shadow_mod.record_profile("找工作中｜催婚", {"职业与求职": "找工作中"}, "shadow")
    check("影子画像落盘", (TMP / "shadow-profile.jsonl").exists())
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

    # ---------- B8. 文件登记：只下一遍，/task 与 chat 共用 ----------
    _src = TMP / "发来的简历.pdf"
    _src.write_bytes(b"%PDF-1.4 fake")
    _dst = files_mod.remember_pending(_src, "发来的简历.pdf")
    check("文件登记写入 agentd 的 inbox", _dst is not None and _dst.exists(), str(_dst))
    try:
        _same_inode = _src.stat().st_ino == _dst.stat().st_ino
    except Exception:  # noqa: BLE001
        _same_inode = False
    check("文件登记用硬链接，不复制内容", _same_inode)
    _info = files_mod.pending()
    check("登记表能被 /task 读到", bool(_info) and _info["path"] == str(_dst), str(_info))
    # 删掉源文件后登记的那份还在：硬链接不是软链接
    _src.unlink()
    check("源文件删了 inbox 那份还在", _dst is not None and _dst.exists())
    files_mod.PENDING_JSON.write_text(
        json.dumps({"path": str(_dst), "name": "x", "ts": 0}), encoding="utf-8")
    check("过期登记当没发过", files_mod.pending() is None)
    files_mod.clear_pending()
    check("清空后登记表为空", files_mod.pending() is None)

    # 收件箱清理：过期删、新的留、正在等派活的不删
    _ib = files_mod.AGENT_INBOX
    (_ib / "old.pdf").write_bytes(b"x" * 100)
    (_ib / "fresh.pdf").write_bytes(b"y" * 10)
    (_ib / "waiting.pdf").write_bytes(b"z" * 10)
    _old = time.time() - 90000      # 25 小时前
    for _n in ("old.pdf", "waiting.pdf"):
        os.utime(_ib / _n, (_old, _old))
    files_mod.PENDING_JSON.write_text(json.dumps(
        {"path": str(_ib / "waiting.pdf"), "name": "waiting.pdf", "ts": time.time()}),
        encoding="utf-8")
    _cr = files_mod.cleanup_inbox(24)
    check("收件箱清理删掉过期文件", _cr["removed"] == 1 and not (_ib / "old.pdf").exists(),
          str(_cr))
    check("收件箱清理保留新文件", (_ib / "fresh.pdf").exists())
    check("正在等派活的过期文件不删", (_ib / "waiting.pdf").exists())
    files_mod.clear_pending()

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

    shadow_mod.ATTENTION_LOG = TMP / "shadow-attention.jsonl"
    shadow_mod.record_attention("我最近在学 Rust", 1.0, 1, 0, "在忙啥")
    check("影子注意力落盘", (TMP / "shadow-attention.jsonl").exists())

    # 影子 → 周报：七路样本要能被压成几句话（以前只能手动跑 ops/shadow-eval.py）
    _all_logs = {
        "followup": TMP / "shadow-followup.jsonl",
        "positive": TMP / "shadow-positive.jsonl",
        "retrieval": TMP / "shadow-retrieval.jsonl",
        "graph": TMP / "shadow-graph.jsonl",
        "profile": TMP / "shadow-profile.jsonl",
        "second": TMP / "shadow-second.jsonl",
        "attention": TMP / "shadow-attention.jsonl",
    }
    # 全部改写到独立的探针目录：别的用例也在用上面那些文件名，
    # 共用会让「恰好 1 行」这类断言在本用例后面炸掉。
    _probe_dir = TMP / "shadow-probe"
    _probe_dir.mkdir(parents=True, exist_ok=True)
    _all_logs = {k: _probe_dir / (k + ".jsonl") for k in _all_logs}
    shadow_mod.FOLLOWUP_LOG = _all_logs["followup"]
    shadow_mod.POSITIVE_LOG = _all_logs["positive"]
    shadow_mod.RETRIEVAL_LOG = _all_logs["retrieval"]
    shadow_mod.GRAPH_LOG = _all_logs["graph"]
    shadow_mod.PROFILE_LOG = _all_logs["profile"]
    shadow_mod.SECOND_LOG = _all_logs["second"]
    shadow_mod.ATTENTION_LOG = _all_logs["attention"]
    shadow_mod.LOGS = dict(_all_logs)
    shadow_mod.record_retrieval("查询", ["a"] * 5, ["b"] * 5, ["a"] * 3 + ["b"] * 2)
    shadow_mod.record_graph("查询", ["a"], ["b", "c"])
    shadow_mod.record_second_recall("那呢", "这个", [{"doc_id": "abcdef12"}])
    shadow_mod.record_positive("笑死我了", "嗯。")
    shadow_mod.record_profile("找工作中", {"职业与求职": "找工作中"}, "on")
    _sum = shadow_mod.weekly_summary(7)
    check("影子汇总：召回对比记到重合数", _sum["retrieval"]["n"] >= 1
          and _sum["retrieval"]["avg_overlap"] > 0, str(_sum["retrieval"]))
    check("影子汇总：连边覆盖率", _sum["graph"]["with_neighbors"] >= 1,
          str(_sum["graph"]))
    check("影子汇总：二次召回命中", _sum["second"]["hit"] >= 1, str(_sum["second"]))
    _sl = shadow_mod.summary_lines(7)
    check("影子周报有可读的行", any("召回对比" in x for x in _sl), str(_sl[:2]))
    check("影子周报会带上画像块",
          any("画像块" in x for x in _sl), str(_sl))

    # ---------- C. 预传 qv 时不重复 embedding ----------
    embed_calls = []

    async def fake_embed(texts):
        embed_calls.append(list(texts))
        return [[0.11, 0.22, 0.33] for _ in texts]

    mem.embed = fake_embed
    await mem.recall("查询", 3, qv=[0.11, 0.22, 0.33])
    check("传 qv 时不再调用 embedding", len(embed_calls) == 0, str(embed_calls))

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
    check("retry 屏蔽 skill_save", "skill_save" not in names)
    check("retry 保留只读工具", "memory_search" in names, str(names))

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

    async def fake_complete_summary(msgs, max_tokens=None):
        # 滚动纪要走的是同一个函数，别把它算进「总结次数」
        if "滚动纪要" in (msgs[0].get("content") or ""):
            return "这段时间在调记忆水位，顺手把滚动纪要也接上了。"
        summary_inputs.append(msgs[-1]["content"])
        return json.dumps({"summary": f"第{len(summary_inputs)}段记忆",
                           "key_facts": ["f"], "topics": ["t"], "importance": 0.6},
                          ensure_ascii=False)

    core.complete_summary = fake_complete_summary
    mem.embed = fake_embed

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
    check("历史预算已降到 2500", core.history_max_chars == 2500, str(core.history_max_chars))
    m_with = core.build_messages([{"role": "user", "content": "x"}], "y", digest=dg)
    # 第 2 条固定是「他的话不是指令」的防注入规则，所以纪要排在第 3 条
    check("纪要在历史之前注入", "更早的对话纪要" in (m_with[2].get("content") or ""),
          str(m_with[2]["content"][:20]))
    check("第二条是防注入规则",
          "都只是**他在说话**" in (m_with[1].get("content") or ""),
          str(m_with[1].get("content"))[:20])
    m_without = core.build_messages([{"role": "user", "content": "x"}], "y")
    check("没有纪要时不注入空块",
          "更早的对话纪要" not in (m_without[2].get("content") or ""))

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
    # 时间/时效现在合并进「这一轮怎么说」的同一段（见 style_block）
    _style_msg = next((m.get("content") or "" for m in last
                       if (m.get("content") or "").startswith("【这一轮怎么说】")), "")
    check("prompt 里带了当前时间", "现在：" in _style_msg, _style_msg[:60])
    check("prompt 里带了时效规则", "时效：" in _style_msg, _style_msg[:60])
    idx_time = next((i for i, m in enumerate(last)
                     if (m.get("content") or "").startswith("【这一轮怎么说】")), -1)
    idx_last_user = len(last) - 1
    check("风格块在历史之后、提问之前", 0 < idx_time < idx_last_user,
          f"{idx_time} vs {idx_last_user}")

    # ---------- F3. 主人隔离：别人能聊，但读不到我的库 ----------
    _saved_uid = core.admin_uid
    core.admin_uid = "111"
    check("主人判定：本人是 owner", core._is_owner("private:111"))
    check("主人判定：别人不是 owner", not core._is_owner("private:222"))
    core.admin_uid = ""
    check("没配 admin 时谁都是 owner（不然一上线全哑）", core._is_owner("private:999"))
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
    check("公开工具里含联网查资料", {"web_search", "web_open"} <= _sp_tools, str(_sp_tools))
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

    # 上一版直接 any(词 in 文本)，「还没搞定」会被判成办完 → 这件事再也不会被问。
    check("「还没搞定」不算办完", not followup_mod.resolve("季度报告还没搞定")["done"])
    check("「没交了」不算办完", not followup_mod.resolve("季度报告没交了")["done"])
    check("否定句不改状态",
          not any(followup_mod.resolve("季度报告还没搞定").values()))

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

    async def bad_summary(msgs, max_tokens=None):
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
    sched_mod.CHAT_DB = str(TMP / "chat.db")
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

    async def fake_note_summary(msgs, max_tokens=None):
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
    check("索引被注入 prompt", any("[你的知识索引]" in (m.get("content") or "")
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

    # ---------- AB. 主动消息判断层（纯函数，可离线测） ----------
    from app_core import proactive as decide
    ok, why = decide.gate(3, 99999, 99999, 0, 4, {}, [0.1, 0.1, 0.1])
    check("夜里不开口", not ok and "时段" in why, why)
    ok, why = decide.gate(15, 60, 99999, 0, 4, {}, [0.1, 0.1, 0.1])
    check("刚发过不开口", not ok and "太近" in why, why)
    ok, why = decide.gate(15, 99999, 60, 0, 4, {}, [0.1, 0.1, 0.1])
    check("刚聊过不打扰", not ok and "刚聊" in why, why)
    ok, why = decide.gate(15, 99999, 99999, 4, 4, {}, [0.1, 0.1, 0.1])
    check("今日已满不开口", not ok and "已满" in why, why)
    ok, why = decide.gate(15, 99999, 99999, 0, 4, {}, [0.9, 0.1, 0.1])
    check("抽签没过不开口", not ok and "抽签" in why, why)
    # 2026-11-05 判据从「我心情好不好」换成「他此刻需不需要有人出现」：
    # mood 不再决定开不开口，只决定怎么说。
    ok, why = decide.gate(15, 99999, 99999, 0, 4,
                          {"valence": -0.8, "energy": 0.2}, [0.1, 0.1, 0.1])
    check("心情差不拦着开口了（只影响语气）", ok and why == "开口", why)
    ok, why = decide.gate(15, 99999, 3600, 0, 4, {}, [0.1, 0.1, 0.1])
    check("刚聊过一小时就别凑上去", not ok and "刚聊" in why, why)
    ok, why = decide.gate(15, 99999, 8 * 3600, 0, 4, {}, [0.1, 0.1, 0.1], threads=2)
    check("他留的话头还悬着就该开口", ok and "话头" in why, why)
    ok, why = decide.gate(15, 99999, 8 * 3600, 0, 4, {}, [0.1, 0.1, 0.1], threads=0)
    check("没话头就按普通时机开口", ok and why == "开口", why)

    check("回应率 0 只给 1 条", decide.daily_cap(0.0) == 1, str(decide.daily_cap(0.0)))
    check("回应率 30% 给 2 条", decide.daily_cap(0.3) == 2)
    check("没有样本给满额", decide.daily_cap(None) == 4)

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

    # ---------- Z3. 配置自检 / 分档阈值 / 归档恢复 / 影子去重 ----------
    # admin.user_id 留空是「不报错但全哑」的失败模式，必须能被检测到
    real_uid = core.admin_uid
    core.admin_uid = ""
    check("admin 没配时自检报出来",
          any("admin.user_id" in m for m in core.config_issues()),
          str(core.config_issues())[:40])
    check("admin 没配时状态行带警告", "未配置" in core.config_status())
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
    _real_perr = chat_mod._PERSONA_ERROR
    chat_mod._PERSONA_ERROR = "system_prompt 为空"
    check("人格异常进自检",
          any("人格" in m for m in core.config_issues()),
          str(core.config_issues())[:80])
    chat_mod._PERSONA_ERROR = _real_perr
    check("人格正常时不报警", not core.config_issues(), str(core.config_issues())[:80])


    # 命令清单：/help 的唯一真相源（lint 会核对注册表，这里只保证内容可用）
    from app_core import help as help_mod
    check("help 清单够全", len(help_mod.names()) >= 36, str(len(help_mod.names())))
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

    # 影子去重：相似度够高时只记日志，绝不合并
    con = sqlite3.connect(TMP / "livingmemory.db")
    row_v = con.execute(
        "SELECT v.doc_id, v.vec, d.metadata FROM memory_vectors v "
        "JOIN documents d ON d.doc_id = v.doc_id").fetchall()
    con.close()
    live = [(d, b) for d, b, m in row_v
            if "archived" not in (m or "")] if row_v else []
    if live:
        import logging as _logging
        import struct as _struct

        same_id, same_blob = live[0]
        n_dim = len(same_blob) // 4
        same_vec = list(_struct.unpack(f"{n_dim}f", same_blob))
        seen_logs: list[str] = []

        class _Cap(_logging.Handler):
            def emit(self, rec):
                seen_logs.append(rec.getMessage())

        cap = _Cap()
        memory_mod.log.addHandler(cap)
        memory_mod.log.setLevel(_logging.INFO)
        try:
            n0 = _doc_count()
            mem.add_document(text="影子去重探针：同一件事", session="private:shadow",
                             importance=0.5, topics=[], key_facts=[], vec=same_vec,
                             range_key="shadow:probe")
        finally:
            memory_mod.log.removeHandler(cap)
        check("影子模式只记日志、不合并（两条都在）", _doc_count() == n0 + 1,
              f"{n0} -> {_doc_count()}")
        check("影子去重打出了命中日志",
              any("记忆去重" in s for s in seen_logs), str(seen_logs[:2])[:80])
    else:
        print("SKIP 影子去重（副本库没有向量）")

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
    # （等待式防抖已删除：实测 14 天 472 次连发里合并成功 0 次，
    #   却让每条消息白等 1.05 秒。现在等待只发生在图片/文件之后。）
    from app_core import turn as turn_mod

    q = turn_mod.TurnQueue()
    q.add("private:1", images=["/tmp/a.jpg"])
    check("图片先到，队列里有货", q.has("private:1"))
    check("另一个会话不受影响", not q.has("private:2"))
    q.add("private:1", text="这是昨天的报错")
    t = q.take("private:1")
    check("图 + 补一句话并成同一轮",
          t.images == ["/tmp/a.jpg"] and t.texts == ["这是昨天的报错"],
          f"{t.images} {t.texts}")
    check("取走之后队列清空", not q.has("private:1"))
    check("计数用于判断窗口内有没有新输入", q.hits("private:1") == 2,
          str(q.hits("private:1")))

    f1 = turn_mod.Turn(file=("报错.log", "堆栈内容" * 100, "文本"))
    f1.merge(turn_mod.Turn(texts=["帮我看看"]))
    prompt_f, stored_f = f1.compose()
    check("文件轮：正文含文件内容与补充说明",
          "报错.log" in prompt_f and "帮我看看" in prompt_f, prompt_f[:60])
    check("文件轮：摘要里也带上补充那句",
          stored_f is not None and "帮我看看" in stored_f and len(stored_f) < 500,
          str(stored_f)[:60])

    check("只有图片时给占位正文",
          turn_mod.Turn(images=["/tmp/a.jpg"]).compose()[0] == "[发了图片]")

    t3 = turn_mod.Turn()
    t3.merge(turn_mod.Turn(texts=["第一句"]))
    t3.merge(turn_mod.Turn(texts=["第二句"]))
    check("连续两条文字按顺序合并", t3.texts == ["第一句", "第二句"], str(t3.texts))

    # ---------- 群聊：限流 / 上下文裁剪 / 隔离 ----------
    from app_core import group as group_mod

    now_g = 1_000_000.0
    check("冷却期内不触发", not group_mod.rate_ok([now_g - 2], now_g, 3, 5))
    check("过了冷却且没超频可以触发", group_mod.rate_ok([now_g - 10], now_g, 3, 5))
    check("一分钟内到上限就停",
          not group_mod.rate_ok([now_g - 10, now_g - 8, now_g - 6], now_g, 3, 1),
          "3 条/分钟")
    check("窗口外的旧记录不再计数",
          group_mod.rate_ok([now_g - 120, now_g - 110], now_g, 3, 5))

    many = [("user", f"第{i}句") for i in range(30)]
    kept = group_mod.trim_context(many, 20, 10 ** 6)
    check("上下文按条数裁剪", len(kept) == 20 and kept[-1][1] == "第29句",
          f"{len(kept)} {kept[-1] if kept else None}")
    kept_c = group_mod.trim_context([("user", "字" * 600)] * 10, 20, 1500)
    total_c = sum(len(t) for _, t in kept_c)
    check("上下文按字数裁剪", total_c <= 1500 and len(kept_c) >= 2,
          f"{len(kept_c)} 条 / {total_c} 字")

    gc = group_mod.GroupCore()
    check("群聊默认关闭", gc.enabled is False)
    check("白名单为空（空跑）", not gc.whitelist, str(gc.whitelist))
    for i in range(25):
        gc.remember("g1", "user", f"第{i}句")
    check("内存上下文不超过 20 条", len(gc.context("g1")) <= 20, str(len(gc.context("g1"))))
    check("不同群上下文隔离", not gc.context("g2"))
    check("群聊没有记忆表依赖", "memory" not in dir(group_mod), "")

    # ---------- 时效：知识保质期 / 自主搜索规则 / 话题巡检 ----------
    from app_core import prompt as prompt_fresh
    fh = prompt_fresh.freshness_hint()
    check("时效规则说明了知识有截止时间", "截止时间" in fh, fh[:40])
    check("时效规则授权它自己搜", "自己搜" in fh, fh[:60])
    check("时效规则要求带日期", "日期" in fh, fh[-40:])

    # ---- 风格块：五条提示合成一条，冲突在一处解决 ----
    _sb = prompt_fresh.style_block
    b1 = _sb("好累", [], {}, [], burst=1)
    check("情绪短句不含时效规则", "时效" not in b1, b1)
    check("情绪短句不含填充式形式", "只做一件事" not in b1, b1)
    check("情绪短句仍给长度", "接一句就够" in b1, b1)
    check("风格块只有一段", b1.count("【这一轮怎么说】") == 1, b1)
    b2 = _sb("这报错怎么改", [], {}, [], images=1)
    check("求助图不压长度", "接一句就够" not in b2 and "可以展开" not in b2, b2)
    check("求助图给求助提示", "他在求助" in b2, b2)
    b3 = _sb("哈哈", [], {}, [], images=1)
    check("分享图给接氛围提示", "接住氛围" in b3, b3)
    check("分享图省掉时效", "时效" not in b3, b3)
    b5 = _sb("为什么 SQLite 的 WAL 模式在多进程下这么容易锁住，我不太理解它的机制",
             [{"role": "assistant", "content": "对，" + "很长" * 40},
              {"role": "assistant", "content": "嗯，" + "很长" * 40}], {})
    check("允许展开时不出现「说短点」", "说短点" not in b5, b5)
    check("允许展开时带时效", "时效" in b5, b5)

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

    class _FakeLLM:
        async def complete(self, *a, **kw):
            return {"content": "哦对，那个还没弄完"}

    _cand = await shadow_mod.record_followup(_FakeLLM(), "在吗", "在")
    check("影子补句生成出来了", "那个还没弄完" in _cand, _cand)
    _fl = (TMP / "shadow-followup.jsonl").read_text(encoding="utf-8").strip().splitlines()
    check("补句落盘", len(_fl) == 1 and "candidate" in _fl[0], str(len(_fl)))

    # ---- 闲聊关思考：只对明确闲聊生效 ----
    check("闲聊短句关思考", core._thinking_for("今天好累", None) is False)
    check("带问号不关", core._thinking_for("你觉得呢？", None) is None)
    check("技术词不关", core._thinking_for("这个报错怎么修", None) is None)
    check("带图不关", core._thinking_for("看这个", ["x.jpg"]) is None)
    check("长句不关", core._thinking_for("我" * 40, None) is None)

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
    _r1 = remind_mod.add("改那个主动回复不进上下文的 bug，先加日志", "2026-11-06 09:00")
    check("第一条提醒能建", "已经有一条" not in _r1, _r1)
    _r2 = remind_mod.add("改那个 bug——主动回复没进上下文，先加日志再复现",
                         "2026-11-06 09:30")
    check("近重复提醒被拦下", "已经有一条" in _r2, _r2)
    _r3 = remind_mod.add("买牛奶", "2026-11-06 09:00")
    check("不同的事照常建", "已经有一条" not in _r3, _r3)

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

    _cold = prompt_fresh.style_block("嗯", [], {}, [], burst=3,
                                     recent_user_lens=[1, 1, 2])
    check("连回短句判为冷淡", "别拉长" in _cold and "可以接一句长的" not in _cold, _cold)
    _warm = prompt_fresh.style_block("嗯", [], {}, [], burst=3,
                                     recent_user_lens=[30, 40, 25])
    check("连发长句仍算兴致", "可以接一句长的" in _warm, _warm)

    # ---- 长回答先说结论 ----
    _deep = prompt_fresh.style_block(
        "为什么 SQLite 的 WAL 在多进程下容易锁，能解释下机制吗", [], {}, [])
    check("要展开时先给结论", "先给结论" in _deep, _deep)

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
    con.close()
    msg_raw = learn_mod.archive_raw("2026-10-05")
    p_raw_day = learn_mod.RAW / "2026-10-05.md"
    check("原文归档写成文件", p_raw_day.exists(), msg_raw)
    txt_raw = p_raw_day.read_text(encoding="utf-8") if p_raw_day.exists() else ""
    check("原文带时间与说话人",
          "[09:30] 他：今天好烦" in txt_raw and "[09:31] 我：怎么了" in txt_raw,
          txt_raw[:50].replace("\n", " "))
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
    # 图片要走「图文比对 + 强度分级」而不是「一律分析」
    check("图片提示要求比对图文", "图在说什么" in text_part and "矛盾" in text_part)
    check("情绪类图不许分析构图", "别分析构图" in text_part)
    check("没配文字默认按分享处理", "默认按分享处理" in text_part)
    check("问东西时要求先搜", "拿不准先搜" in text_part)
    check("排错场景仍要求逐字转录", "逐字转录关键行" in text_part)
    check("矛盾时不拆穿反讽", "别拆穿反讽" in text_part)
    check("回应强度有 L1/L2/L3 分级", "L1 认可" in text_part and "L3 情感推断" in text_part)
    check("证据不足必须降级", "证据不足就降级" in text_part)
    check("氛围图禁止信息提取式回复", "图中显示" in text_part)
    check("多图按组回应", "别逐张点评" in text_part)
    check("判错时按他的纠正来", "按他下一句纠正" in text_part)

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
    _saved_corr = learn_mod.CORRECTIONS
    learn_mod.CORRECTIONS = _P(TMP) / "corrections.md"
    try:
        learn_mod.write_list(learn_mod.CORRECTIONS, "他纠正过我的",
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
        _saved_w = (learn_mod.LEARN, learn_mod.CORRECTIONS,
                    learn_mod.CURIOUS, learn_mod.PROFILE)
        learn_mod.LEARN = _P(TMP) / "learn_w"
        learn_mod.LEARN.mkdir(parents=True, exist_ok=True)
        learn_mod.CORRECTIONS = learn_mod.LEARN / "corrections.md"
        learn_mod.CURIOUS = learn_mod.LEARN / "curious.md"
        learn_mod.PROFILE = learn_mod.LEARN / "self_profile.md"
        try:
            res_w = learn_mod.apply_review({
                "learned": "当…时，做…", "profile": "我是他手机里的熟人",
                "corrections": ["[图片] 别瞎猜糊图", "别一口气发太长", "[文字] 别催我"],
                "curious": ["还没搞懂 X"], "resolved_curious": []},
                day="2026-11-03")
            raw_corr = learn_mod.CORRECTIONS.read_text(encoding="utf-8")
            check("复盘写入的纠正保留原标签", "[图片] 别瞎猜糊图" in raw_corr, raw_corr[:40])
            check("没标签的纠正被补成通用", "[通用] 别一口气发太长" in raw_corr, raw_corr[-30:])
            check("apply_review 返回纠正条数", res_w["corrections"] == 3, str(res_w))
            check("复盘日记被写出", (learn_mod.LEARN / "2026-11-03.md").exists())
            check("自我认知被更新",
                  "熟人" in learn_mod.PROFILE.read_text(encoding="utf-8"))
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
            learn_mod.write_list(learn_mod.CORRECTIONS, "他纠正过我的", _seed)
            _kept = learn_mod.merge_list(
                learn_mod.CORRECTIONS, "他纠正过我的", ["[通用] 全新的一条完全不同的规则"])
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
            (learn_mod.LEARN, learn_mod.CORRECTIONS,
             learn_mod.CURIOUS, learn_mod.PROFILE) = _saved_w
    finally:
        learn_mod.CORRECTIONS = _saved_corr

    # ---- 图片图注：追加到原消息，下一轮就能看到 ----
    _rid = await core.remember("private:imgtest", "user", "[发了图片]（附了 1 张图）")
    check("remember 返回行号", _rid > 0, str(_rid))
    _ok = await core.append_to_message(_rid, "｜图注：猫趴在键盘上")
    _con = sqlite3.connect(TMP / "chat.db")
    _row = _con.execute("SELECT content FROM messages WHERE id=?", (_rid,)).fetchone()
    _con.close()
    check("图注能追加到原消息",
          _ok and _row and "图注：猫趴在键盘上" in _row[0], str(_row))

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

    with patch("httpx.AsyncClient", _EmbedClient):
        res = await memory_mod.Memory().embed(["", "  ", "正常文本"])
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

    # ---------- Z16. 发送节奏：长回答要自动加速 ----------
    from app_core import pacing as pacing_mod

    slow = [pacing_mod.gap_seconds(i) for i in range(5)]
    fast = [pacing_mod.gap_seconds(i) for i in range(9, 20)]
    check("短回答保持原节奏", all(1.2 <= x <= 2.8 for x in slow), str([round(x, 2) for x in slow]))
    check("段数一多自动加速", all(x <= 1.0 for x in fast), str([round(x, 2) for x in fast]))
    check("已知总段数多也加速", pacing_mod.gap_seconds(0, 20) <= 1.0,
          str(round(pacing_mod.gap_seconds(0, 20), 2)))
    check("is_fast 判定", pacing_mod.is_fast(8) and not pacing_mod.is_fast(3))

    p_slow = pacing_mod.pace_seconds("一句话", 15, {"energy": 0.7})
    p_fast = pacing_mod.pace_seconds("一句话", 15, {"energy": 0.7}, fast=True)
    check("加速时打字停顿有上限", p_fast <= pacing_mod.FAST_PACE_CAP + 1e-9,
          f"{p_slow:.2f} → {p_fast:.2f}")
    check("夜里更慢", pacing_mod.pace_seconds("一句话", 2, {"energy": 0.7})
          > pacing_mod.pace_seconds("一句话", 15, {"energy": 0.7}))
    check("节奏有下限", pacing_mod.pace_seconds("", 15, {"energy": 0.1}) >= 0.15)

    # 回复预算：图片轮和工具轮都按 1600 的下限
    check("回复输出预算下限 1600", chat_mod.REPLY_MAX_TOKENS == 1600,
          str(chat_mod.REPLY_MAX_TOKENS))

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
    fake_par = _NS(TOOLS=[], execute=timed_exec, READ_ONLY_TOOLS={"web_search"})
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
    fake_seq = _NS(TOOLS=[], execute=timed_exec, READ_ONLY_TOOLS={"web_search"})
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

    fake_dup = _NS(TOOLS=[], execute=count_exec, READ_ONLY_TOOLS={"web_search"})
    await chat_mod.tool_loop.run(
        chat_mod, [], complete=make_complete([dup]), stream=make_complete([dup]),
        tools_mod=fake_dup, tools_banned=set(), max_rounds=2,
        tool_timeout=5, total_timeout=5, stats={})
    check("同轮重复调用只跑一次", ran["n"] == 1, str(ran))

    # 还原测试期替换掉的真实现
    chat_mod.tools.execute = REAL_TOOLS_EXECUTE

    # 收尾：取消遗留的后台任务（reply 会调度后台记忆总结），避免 loop 关闭后报错
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    for t in pending:
        t.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


try:
    asyncio.run(main())
finally:
    # 临时库跑完就删。不删的话每跑一次就在当前目录留一个 qqbot-test-*，
    # 本地攒几天就分不清哪个是本轮的（云端 /tmp 会自动清，本地不会）。
    import shutil

    shutil.rmtree(TMP, ignore_errors=True)

passed = sum(1 for r in RESULTS if r)
print(f"\n=== {passed}/{len(RESULTS)} 通过 ===")
sys.exit(0 if passed == len(RESULTS) else 1)
