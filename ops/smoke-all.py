"""全链路体检：把 app_core 里每个函数都实际调用一遍（外部依赖全部 mock）。

目的不是替代离线回归（那 229 项测「行为对不对」），而是**覆盖率体检**：
哪些函数从来没被任何测试碰过、一碰就炸。

用法：
    QQBOT_CODE_DIR=<仓库>/nonebot python ops/smoke-all.py

约定：不联网、不写生产库、不发消息。所有落盘路径都指到临时目录。
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

CODE = Path(os.environ.get("QQBOT_CODE_DIR", "/opt/nonebot")).resolve()
sys.path.insert(0, str(CODE))

TMP = Path(tempfile.mkdtemp(prefix="smoke-"))
RESULTS: list[tuple[str, bool, str]] = []
VEC = [0.1, 0.2, 0.3]


def check(name: str, ok: bool, extra: str = "") -> None:
    RESULTS.append((name, bool(ok), extra))
    print(("PASS " if ok else "FAIL ") + name + ((" | " + extra) if extra else ""))


async def call(name, fn, *a, **kw):
    try:
        out = fn(*a, **kw)
        if asyncio.iscoroutine(out):
            out = await out
        check(name, True, str(out)[:46].replace("\n", " "))
        return out
    except Exception as e:  # noqa: BLE001
        check(name, False, f"{type(e).__name__}: {e}")
        return None


# ---------- 假 HTTP：给 web / embed / 图片下载用 ----------
class FakeResp:
    def __init__(self, body: bytes = b"", data=None, status: int = 200, url: str = ""):
        self._body, self._data, self.status_code = body, data, status
        self.url, self.encoding = url, "utf-8"

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("http %s" % self.status_code)

    def json(self):
        return self._data or {}

    async def aiter_bytes(self):
        yield self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


HTML = ("<html><head><title>标题</title></head><body><p>" + "正文" * 300 + "</p></body></html>")


def _png_bytes() -> bytes:
    from io import BytesIO

    from PIL import Image

    buf = BytesIO()
    Image.new("RGB", (64, 64), (10, 20, 30)).save(buf, format="PNG")
    return buf.getvalue()


class FakeClient:
    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def stream(self, method, url):
        if url.endswith(".png"):
            return FakeResp(_png_bytes(), url=url)
        return FakeResp(HTML.encode("utf-8"), url=url)

    async def get(self, url, **kw):
        if url.startswith("data:image"):
            return FakeResp(base64.b64decode(url.split(",", 1)[1]))
        return FakeResp(HTML.encode("utf-8"), url=url)

    async def post(self, url, **kw):
        if "embeddings" in url:
            n = len((kw.get("json") or {}).get("input") or [])
            return FakeResp(data={"data": [{"index": i, "embedding": VEC} for i in range(n)]})
        if "search" in url:
            return FakeResp(data={"results": [{"title": "T", "url": "http://x", "summary": "S"}]})
        return FakeResp(data={"choices": [{"message": {"content": "好。"}}]})


# ---------- 路径重定向 ----------
from app_core import (  # noqa: E402
    chat as chat_mod, diary as diary_mod, files as files_mod, followup as fu_mod,
    intent as intent_mod, kb as kb_mod, learn as learn_mod,
    meme as meme_mod, memory as memory_mod,
    mood as mood_mod, remind as remind_mod, retrieval as retrieval_mod,
    memgraph as memgraph_mod, scheduler as sched_mod,
    shadow as shadow_mod, summarize as summarize_mod,
    attention as attention_mod, relational as relational_mod,
    profile as profile_mod, contradict as contradict_mod, help as help_mod,
    maintenance as maint_mod,
    textsim as textsim_mod, tools as tools_mod, vocab as vocab_mod,
    web as web_mod, wiki as wiki_mod,
)

LEARN = TMP / "learn"
for p in (LEARN, LEARN / "notes", LEARN / "skills", LEARN / "raw"):
    p.mkdir(parents=True, exist_ok=True)

chat_mod.DB, chat_mod.DATA = TMP / "chat.db", TMP
memory_mod.DB = TMP / "livingmemory.db"
kb_mod.DB, kb_mod.LEARN = TMP / "chat.db", LEARN
learn_mod.LEARN, learn_mod.MEM_DB, learn_mod.CHAT_DB = LEARN, memory_mod.DB, TMP / "chat.db"
learn_mod.PROFILE, learn_mod.CORRECTIONS = LEARN / "self_profile.md", LEARN / "corrections.md"
learn_mod.CURIOUS = LEARN / "curious.md"
learn_mod.SKILLS, learn_mod.NOTES, learn_mod.RAW = LEARN / "skills", LEARN / "notes", LEARN / "raw"
wiki_mod.DB, wiki_mod.NOTES, wiki_mod.SKILLS = TMP / "chat.db", learn_mod.NOTES, learn_mod.SKILLS
diary_mod.DIARY_DIR, diary_mod.MIRROR_DIR, diary_mod.CHAT_DB = TMP / "diary", LEARN, TMP / "chat.db"
contradict_mod.LOG = TMP / "contradictions.jsonl"
meme_mod.PACKS = TMP / "packs"
meme_mod.META = meme_mod.PACKS / "pack" / "semantic_metadata.json"
meme_mod.CHAT_DB = TMP / "chat.db"
fu_mod.DB = TMP / "chat.db"
remind_mod.DB = TMP / "chat.db"
mood_mod.STATE_DIR, mood_mod.STATE = TMP / "mood", TMP / "mood" / "state.json"
mood_mod.CHAT_DB = TMP / "chat.db"
files_mod.SAVE_DIR, files_mod.IMAGE_DIR = TMP / "files", TMP / "files" / "images"
files_mod.PENDING_JSON = TMP / "last-file.json"
files_mod.AGENT_INBOX = TMP / "agent-inbox"
sched_mod.CHAT_DB = str(TMP / "chat.db")
sched_mod.CLEANUP_LOG = str(TMP / "cleanup.log")

con = sqlite3.connect(memory_mod.DB)
con.executescript((CODE / "app_core" / "memory_schema.sql").read_text(encoding="utf-8"))
con.commit()
con.close()
memory_mod.Memory().init()

from PIL import Image  # noqa: E402

meme_mod.META.parent.mkdir(parents=True, exist_ok=True)
for i in range(2):
    Image.new("RGB", (32, 32), (i * 80, 100, 200)).save(meme_mod.PACKS / f"m{i}.png")
meme_mod.META.write_text(json.dumps({
    "images": {
        "m0.png": {"relative_path": "m0.png", "caption": "开心 笑", "tags": ["开心"],
                   "category": "happy"},
        "m1.png": {"relative_path": "m1.png", "caption": "无语 叹气", "tags": ["无语"],
                   "category": "sad"},
    }
}), encoding="utf-8")


async def fake_embed(texts):
    return [VEC for _ in texts]


class FakeCore:
    def __init__(self):
        self.admin_uid = "10001"
        self.memory = memory_mod.Memory()
        self.memory.embed = fake_embed
        self.persona = {"system_prompt": "测试人格", "begin_dialogs": []}

    async def complete_raw(self, msgs, tools_arg=None, **kw):
        return {"content": "好。", "tool_calls": []}

    async def complete(self, msgs, max_tokens=None):
        return "好。"

    async def complete_summary(self, msgs, max_tokens=None):
        return json.dumps({
            "summary": "他说要交周报", "importance": 0.8, "topics": ["工作"],
            "key_facts": ["交周报"], "followups": ["交周报"], "learned": "无",
            "profile": "测试", "corrections": [], "curious": [], "diary": "今天聊了周报",
        }, ensure_ascii=False)

    async def record_assistant(self, *a, **kw):
        pass


core = FakeCore()


async def main() -> None:
    import aiosqlite

    async with aiosqlite.connect(TMP / "chat.db") as db:
        await db.execute("CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                         "session TEXT, role TEXT, content TEXT, ts REAL)")
        await db.commit()
    await sched_mod.Scheduler(core)._ensure_schema()
    day = time.strftime("%Y-%m-%d")

    async def remember(role, content):
        async with aiosqlite.connect(TMP / "chat.db") as db:
            await db.execute("INSERT INTO messages(session, role, content, ts) VALUES (?,?,?,?)",
                             ("private:10001", role, content, time.time()))
            await db.commit()

    for i in range(8):
        await remember("user", f"第{i}句：周报还没写完")
        await remember("assistant", f"回第{i}句")

    with patch("httpx.AsyncClient", FakeClient):
        # ---------- diary ----------
        await call("diary.write_day", diary_mod.write_day, core, day, True)
        check("diary 落盘", (TMP / "diary" / f"{day}.md").exists())
        check("diary.read", bool(diary_mod.read(day)))
        check("diary.list_days", day in diary_mod.list_days(3))
        rows = diary_mod._day_messages(day)
        check("diary._day_messages", len(rows) >= 10, str(len(rows)))
        check("diary.format_rows", "第" in diary_mod.format_rows(rows))
        diary_mod.save("2026-01-01", "补写的日记")

        # ---------- meme ----------
        meme_mod.init()
        packs = meme_mod.scan()
        check("meme.scan", len(packs) == 2, str(len(packs)))
        check("meme.meta", bool(meme_mod.meta()))
        check("meme.stats", meme_mod.stats().get("total") == 2, str(meme_mod.stats())[:36])
        await call("meme.ensure_vectors", meme_mod.ensure_vectors)
        pick = await call("meme.find", meme_mod.find, "开心")
        check("meme.find 有结果", bool(pick), str(pick)[:36])
        check("meme._category_fallback", bool(meme_mod._category_fallback("无语")))
        check("meme._pick", isinstance(meme_mod._pick([str(x[1]) for x in packs]), str))
        check("meme.describe", isinstance(meme_mod.describe(pick), str))
        check("meme.categories", isinstance(meme_mod.categories(), dict))

        # ---------- learn ----------
        learn_mod.write_list(learn_mod.CURIOUS, "还没搞懂", ["问题一", "问题二"])
        check("learn.read_list", "问题一" in learn_mod.read_list(learn_mod.CURIOUS))
        learn_mod.merge_list(learn_mod.CURIOUS, "还没搞懂", ["问题三"], cap=5)
        check("learn.merge_list", "问题三" in learn_mod.read_list(learn_mod.CURIOUS))
        check("learn.resolve_curious", learn_mod.resolve_curious(["问题一"]) >= 0)
        check("learn._extract_json", learn_mod._extract_json('{"a": 1}') == {"a": 1})
        check("learn._documents_since", isinstance(learn_mod._documents_since(7), list))
        check("learn._day_context", "情绪" in learn_mod._day_context(1))
        # 复盘要有「今天的新记忆」才会真的跑，先塞一条
        core.memory.add_document("他今天说要交周报", "private:10001", 0.8, ["工作"],
                                 ["交周报"], vec=VEC)
        await call("learn.review", learn_mod.review, core)
        check("learn.review 产出", (LEARN / f"{day}.md").exists())
        await call("learn.weekly", learn_mod.weekly, core)
        cands = learn_mod.review_candidates()
        check("learn.review_candidates", isinstance(cands, list), str(len(cands)))
        await call("learn.reinforce", learn_mod.reinforce, [])
        await call("learn.spaced_review", learn_mod.spaced_review, core)
        s1 = await call("learn.skill_save", learn_mod.skill_save, "体检技能", "体检,smoke", "第一步")
        check("learn.skill_save 返回", "体检技能" in str(s1), str(s1))
        check("learn.skill_list", "体检技能" in learn_mod.skill_list())
        check("learn.skill_read", "第一步" in learn_mod.skill_read("体检技能"))
        check("learn.skill_match", bool(learn_mod.skill_match("做个体检 smoke")))
        learn_mod.note_write("体检笔记", "正文：验证一下。")
        check("learn.note_list", bool(learn_mod.note_list()))
        await call("learn.archive_raw", learn_mod.archive_raw, day)

        # ---------- intent（输入语义，派活时用） ----------
        check("intent 认出动手类", intent_mod.wants_task("把第二段改成黑体"))
        check("intent 不把闲聊当任务", not intent_mod.wants_task("今天好累"))
        check("intent 认出具名文件",
              intent_mod.mentioned_files("改一下 报告.docx 的标题") == ["报告.docx"])
        check("intent 认出指代", intent_mod.refers_to_previous("那个 PPT 改一下"))
        check("intent 有主题词", bool(intent_mod.keywords("把那个 PPT 第三页的标题改成黑体")))
        check("intent 画像结构完整", set(intent_mod.analyze("改一下 x.docx")) >=
              {"task", "files", "refers", "terms"})

        # ---------- shadow（只记日志，不发消息） ----------
        check("shadow 能识别正反馈", shadow_mod.positive_signal("哈哈笑死"))
        check("shadow 不误判普通句", not shadow_mod.positive_signal("明天几点"))
        shadow_mod.FOLLOWUP_LOG = TMP / "smoke-shadow-followup.jsonl"
        shadow_mod.POSITIVE_LOG = TMP / "smoke-shadow-positive.jsonl"
        shadow_mod.RETRIEVAL_LOG = TMP / "smoke-shadow-retrieval.jsonl"
        shadow_mod.record_positive("笑死了", "嗯")
        check("shadow 正例落盘",
              (TMP / "smoke-shadow-positive.jsonl").exists())

        # ---------- textsim（提醒/跟进的去重共用） ----------
        check("textsim.norm 去标点",
              textsim_mod.norm("改那个 bug，先加日志。") == "改那个bug先加日志")
        check("textsim.similar 同义句高分",
              textsim_mod.similar("改那个 bug 先加日志",
                                  "改那个主动回复不进上下文的 bug，先加日志再复现") > 0.6)
        check("textsim.similar 无关句低分",
              textsim_mod.similar("买牛奶", "准备面试材料") < 0.2)
        check("textsim.similar 空串为 0", textsim_mod.similar("", "x") == 0.0)

        # ---------- retrieval（BM25 关键词通道 / RRF 融合） ----------
        check("retrieval.tokenize 中文 bigram",
              retrieval_mod.tokenize("天气") == ["天气"])
        bm = retrieval_mod.BM25(["杭州出差", "在学 Rust", "今天天气不错"])
        check("retrieval.BM25 能排出一名",
              bm.ranked("天气") == [2], str(bm.ranked("天气")))
        check("retrieval.BM25 空库不炸", retrieval_mod.BM25([]).ranked("x") == [])
        check("retrieval.rrf 融合两路",
              max(retrieval_mod.rrf([["a"], ["a"]]), key=lambda k: k) == "a")
        check("retrieval.fuse 单路退化",
              retrieval_mod.fuse([], ["z"], limit=3) == ["z"])
        shadow_mod.record_retrieval("天气怎么样", dense=["a1b2c3d4"],
                                    bm25=["e5f6a7b8"], fused=["e5f6a7b8", "a1b2c3d4"])
        check("shadow 召回对比落盘", (TMP / "smoke-shadow-retrieval.jsonl").exists())

        # ---------- memgraph（共现实体连边） ----------
        shadow_mod.GRAPH_LOG = TMP / "smoke-shadow-graph.jsonl"
        gg = memgraph_mod.EntityGraph([(1, ["装机", "内存"]), (2, ["装机"])])
        check("memgraph 建图并连边",
              [p for p, _ in gg.related([1])] == [2], str(gg.stats()))
        check("memgraph 归一化",
              memgraph_mod.normalize(" A  架构 ") == memgraph_mod.normalize("a 架构"))
        shadow_mod.record_graph("装机", dense=["x"], expanded=["y", "x"])
        check("shadow 连边落盘", (TMP / "smoke-shadow-graph.jsonl").exists())

        # ---------- vocab（主题词表复用） ----------
        check("vocab 近义归并",
              vocab_mod.canonicalize(["求职面试"], ["求职"]) == ["求职"])
        check("vocab 频次建表", vocab_mod.build([["a", "b"], ["a"]], 5)[0] == "a")
        check("summarize.vocab_hint 可拼装",
              "复用" in summarize_mod.vocab_hint(["求职"]))

        # ---------- profile（自我画像字段化） ----------
        check("profile.parse 接收 dict",
              profile_mod.parse({"家庭": "催婚"}) == {"家庭": "催婚"})
        check("profile.parse 拒绝散文", profile_mod.parse("随便一段话") == {})
        check("profile.merge 保留旧字段",
              profile_mod.merge({"家庭": "催婚"}, {"职业": "x"}).get("职业") == "x")
        check("profile.compact 可用", profile_mod.compact({"家庭": "催婚"}) == "催婚")
        _st = profile_mod.update_state(
            {"家庭": "催婚"}, {"家庭": 0.9}, {}, {},
            {"家庭": "催婚了"}, {"家庭": 0.5}, {}, {"家庭": False}, 1_800_000_000.0)
        check("profile.update_state 复述不改写",
              _st["fields"]["家庭"] == "催婚" and not _st["previous"])
        _st2 = profile_mod.update_state(
            {"家庭": "催婚"}, {"家庭": 0.9}, {}, {},
            {"家庭": "已订婚"}, {"家庭": 0.9}, {"家庭": "上个月订的"},
            {"家庭": True}, 1_800_000_000.0)
        check("profile.update_state 变化留旧值",
              _st2["previous"]["家庭"]["text"] == "催婚")

        # ---------- maintenance（月度维护） ----------
        _mf = TMP / "maint-log.jsonl"
        _mf.write_text(
            json.dumps({"ts": time.time() - 40 * 86400, "x": 1}) + "\n"
            + json.dumps({"ts": time.time(), "x": 2}) + "\n", encoding="utf-8")
        _tr = maint_mod.trim_jsonl(_mf, 30)
        check("maintenance 截断老日志", _tr["dropped"] == 1 and _tr["kept"] == 1, str(_tr))

        # ---------- help（命令清单） ----------
        _hn = help_mod.names()
        check("help 清单非空且不重复",
              len(_hn) > 20 and len(_hn) == len(set(_hn)), f"{len(_hn)} 条")
        check("help 文案带斜杠", help_mod.text().count("/") >= len(_hn))

        # ---------- contradict（矛盾检测） ----------
        check("contradict.parse 认得 pairs",
              contradict_mod.parse('{"pairs":[{"old":"x","action":"supersede","why":"入职"}]}')[0]["old"] == "x")
        check("contradict 低相似度不改",
              contradict_mod.should_supersede(
                  {"action": "supersede", "why": "入职"}, 0.55, 0) == (False, "low-similarity"))
        contradict_mod.record({"action": "supersede", "why": "入职"},
                              {"doc_id": "aaaa1111", "text": "旧事", "score": 0.8},
                              True, "ok")
        check("矛盾判定落盘", (TMP / "contradictions.jsonl").exists())
        from app_core import prompt as prompt_mod

        _pm = prompt_mod.build_messages({"system_prompt": "x"}, [], "在", profile="催婚")
        check("prompt 带画像块", any("[关于他]" in m["content"] for m in _pm))
        shadow_mod.PROFILE_LOG = TMP / "smoke-shadow-profile.jsonl"
        shadow_mod.SECOND_LOG = TMP / "smoke-shadow-second.jsonl"
        shadow_mod.record_profile("催婚", {"家庭": "催婚"})
        shadow_mod.record_second_recall("那呢", "这个", [{"doc_id": "aaaa1111"}])
        check("影子画像/二次召回落盘",
              (TMP / "smoke-shadow-profile.jsonl").exists()
              and (TMP / "smoke-shadow-second.jsonl").exists())
        check("relational 还在聊", "还在聊" in relational_mod.describe({}, 0.5))
        check("relational 有话头", "还没说透" in relational_mod.describe({}, 8, 1, "那个 bug"))
        check("attention 自指率", attention_mod.self_ref_ratio("你吃了吗")["ratio"] == 0.0)
        shadow_mod.ATTENTION_LOG = TMP / "smoke-attention.jsonl"
        shadow_mod.record_attention("我最近在学 Rust", 1.0, 1, 0, "?")
        check("影子注意力落盘", (TMP / "smoke-attention.jsonl").exists())
        check("纠正去重按罕见短语合并",
              len(learn_mod.dedupe_items([
                  "[文字] 我谎称记着了但实际没存，别装。",
                  "[通用] 我说记着了但实际没存，别装。"])) == 1)

        # ---------- turn / group / watch / purge（2026-11-03 新增的四个模块） ----------
        from app_core import turn as turn_mod
        from app_core import group as group_mod
        from app_core import watch as watch_mod
        from app_core import purge as purge_mod

        async def _ret(v):
            return v

        tq = turn_mod.TurnQueue()
        tq.add("g1", text="在吗")
        tq.add("g1", images=["/tmp/a.jpg"])
        tq.add("g1", file=("报错.log", "堆栈" * 10, "文本"))
        check("turn.has", tq.has("g1"))
        check("turn.peek", tq.peek("g1") is not None)
        check("turn.hits", tq.hits("g1") == 3, str(tq.hits("g1")))
        _t = tq.take("g1")
        check("turn.take 取走并清空", not tq.has("g1") and not _t.empty())
        check("turn.compose 出正文", isinstance(_t.compose()[0], str))
        _t2 = turn_mod.Turn()
        _t2.merge(turn_mod.Turn(texts=["补充"]))
        check("turn.empty", not _t2.empty())
        tq.drop("g1")
        check("turn.drop", tq.hits("g1") == 0)

        check("group.rate_ok", group_mod.rate_ok([], time.time(), 3, 5))
        check("group.trim_context",
              len(group_mod.trim_context([("user", "x")] * 5, 3, 999)) == 3)
        gc = group_mod.GroupCore()
        gc.remember("g1", "user", "你好")
        check("group.context", len(gc.context("g1")) == 1)
        check("group.build", gc.build("g1", "在吗")[-1]["role"] == "user")
        check("group.tokens_today", isinstance(gc.tokens_today(), int))
        check("group.budget_left", isinstance(gc.budget_left(), bool))

        _saved_watch = watch_mod.WATCH
        watch_mod.WATCH = TMP / "watchlist.json"
        try:
            check("watch.seed", watch_mod.seed(["显卡价格"]) >= 0)
            watch_mod.save([{"topic": "显卡价格", "last": "", "ts": 0}])
            check("watch.load", len(watch_mod.load()) == 1)
            _wc = await call("watch.check_one", watch_mod.check_one,
                             watch_mod.load()[0],
                             lambda q, n=5: _ret("搜索结果：（2026-11-03）2999"),
                             lambda m, t=None: _ret("降价了 200"))
            check("watch.check_one 返回变化", "降价" in str(_wc), str(_wc)[:30])
            _wa = await call("watch.check_all", watch_mod.check_all,
                             lambda q, n=5: _ret("搜索结果：（2026-11-03）2999"),
                             lambda m, t=None: _ret("无"))
            check("watch.check_all", isinstance(_wa, list))
        finally:
            watch_mod.WATCH = _saved_watch

        _saved_bk = purge_mod.BACKUP_DIR
        purge_mod.BACKUP_DIR = TMP / "bk"
        try:
            purge_mod.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
            _f1 = TMP / "p1.md"
            _f1.write_text("内容一", encoding="utf-8")
            _pack = purge_mod.pack([_f1], "t")
            check("purge.pack 产出可读的包", _pack is not None and _pack.exists(), str(_pack))
            check("purge.select_candidates", isinstance(purge_mod.select_candidates([]), list))
            for i in range(4):
                _f = purge_mod.BACKUP_DIR / f"code-{i}.tgz"
                _f.write_bytes(b"x")
            check("purge.rotate", isinstance(purge_mod.rotate(("code-",), keep=2), list))
            check("purge.prune_old_packs", isinstance(purge_mod.prune_old_packs(1), list))
            check("purge.purge_knowledge 空候选直接返回",
                  purge_mod.purge_knowledge(180, 5)["deleted"] == 0)
        finally:
            purge_mod.BACKUP_DIR = _saved_bk

        # ---------- prompt / pacing / proactive / summarize / tool_loop / config / llm ----------
        # 这七个模块以前只被间接调用（经 chat），没有直接体检项——
        # link-check 现在会查「每个模块都进了 smoke-all」，所以补上。
        from app_core import prompt as prompt_mod
        from app_core import pacing as pacing_mod
        from app_core import proactive as pro_mod
        from app_core import summarize as sum_mod
        from app_core import tool_loop as tl_mod
        from app_core import llm as llm_mod
        from app_core import config as config_mod

        check("prompt.clip", prompt_mod.clip("abcdef", 3).endswith("…"))
        check("prompt.age_label", prompt_mod.age_label(3).startswith("3"))
        _sp = prompt_mod.split_reply("你好。再见。", 3)
        check("prompt.split_reply", len(_sp) == 2 and _sp[0] == "你好。", str(_sp))
        check("prompt.shape_hint", isinstance(prompt_mod.shape_hint([], {}), str))
        check("prompt.now_line", prompt_mod.now_line().startswith("[现在]"))
        check("prompt.freshness_hint", "时效" in prompt_mod.freshness_hint())
        check("prompt.length_hint", isinstance(prompt_mod.length_hint("在吗", {}, []), str))
        check("prompt.build_messages",
              prompt_mod.build_messages({}, [], "hi")[-1]["role"] == "user")
        check("pacing.gap_seconds", pacing_mod.gap_seconds(0) > 0)
        check("pacing.pace_seconds", pacing_mod.pace_seconds("x", 12) >= 0.15)
        check("pacing.is_fast", not pacing_mod.is_fast(1))
        _gate = pro_mod.gate(10, 9999, 9999, 0, 3, {}, [0.1, 0.1, 0.1])
        check("proactive.gate", isinstance(_gate[0], bool) and isinstance(_gate[1], str))
        check("proactive.daily_cap", pro_mod.daily_cap(None) >= 1)
        check("proactive.pick_topic",
              pro_mod.pick_topic([], [], [], set(), 0.1, 0.1) is None)
        check("summarize.parse", sum_mod.parse('{"summary":"x"}')["summary"] == "x")
        check("summarize.format_dialog",
              "他" in sum_mod.format_dialog([("user", "你好")]))
        check("config.load", isinstance(config_mod.load(), dict))
        check("llm._retryable", llm_mod._retryable(RuntimeError()) is False)
        _tl_out = await call("tool_loop.run", tl_mod.run,
                             SimpleNamespace(mood_event=None), [],
                             complete=lambda m, t: _ret({"content": "好"}),
                             stream=lambda m, t, cb: _ret({"content": "好"}),
                             tools_mod=SimpleNamespace(TOOLS=[]),
                             tools_banned=set(), max_rounds=1,
                             tool_timeout=1, total_timeout=1)
        check("tool_loop.run 返回正文", _tl_out == "好", str(_tl_out))

        # ---------- followup ----------
        fu_mod.add("体检待办", "memory", "doc-1")
        fu_mod.add_many(["买咖啡豆", "整理报销发票"], "memory", "doc-2")
        pend = fu_mod.pending(10)
        check("followup.pending", len(pend) >= 3, str(len(pend)))
        ids = [x["id"] for x in pend]
        check("followup._overlap", isinstance(fu_mod._overlap("交周报", "周报交了"), float))
        check("followup.mark_done", fu_mod.mark_done(ids[:1]) >= 1)
        fu_mod.set_status(ids[1], "doing")
        fu_mod.mark_asked(ids[1:2])
        check("followup.due_for_asking", isinstance(fu_mod.due_for_asking(), list))
        check("followup.describe_statuses", isinstance(fu_mod.describe_statuses(), dict))
        check("followup.texts_for", isinstance(fu_mod.texts_for(ids[1:2]), list))
        fu_mod.confirm_pending("嗯，那个办完了")
        res = fu_mod.resolve("那个体检待办二已经搞定了")
        check("followup.resolve", isinstance(res, dict) and bool(res), str(res)[:50])
        fu_mod.apply(res)
        check("followup.expire", isinstance(fu_mod.expire(), dict))

        # ---------- tools 全部分发 ----------
        for tname, args in [
            ("note_write", {"title": "工具笔记", "content": "正文", "tag": "体检"}),
            ("note_list", {}),
            ("skill_save", {"name": "工具技能", "trigger": "工具,体检", "steps": "一步"}),
            ("skill_list", {}),
            ("skill_read", {"name": "工具技能"}),
            ("web_search", {"query": "test"}),
            ("web_open", {"url": "https://example.com"}),
            ("diary_read", {}),
            ("kb_read", {"title": "工具笔记"}),
            ("kb_search", {"query": "体检"}),
            ("memory_search", {"query": "周报"}),
            ("memory_read", {"query": "周报"}),
            ("remind_me", {"text": "喝水", "when": time.strftime(
                "%Y-%m-%d %H:%M", time.localtime(time.time() + 10 * 86400))}),
            ("remind_list", {}),
            ("remind_cancel", {"id": 999999}),
            ("meme_send", {"query": "开心"}),
        ]:
            out = await call(f"tools.{tname}", tools_mod.execute, core, tname, args, [])
            check(f"tools.{tname} 有返回", out is not None, str(out)[:36])

        # ---------- kb / wiki / memory ----------
        await call("kb.index", kb_mod.index)
        check("kb._is_curated", isinstance(kb_mod._is_curated(str(LEARN / "notes" / "x.md")), bool))
        hits = await call("kb.search_meta", kb_mod.search_meta, "体检")
        check("kb.search_meta 结构", isinstance(hits, list))
        note_path = next(iter(learn_mod.NOTES.glob("*体检笔记*.md")), None)
        wiki_mod.set_verified(note_path, time.time(), "active")
        check("wiki.set_verified", True)
        check("wiki._fallback_title", bool(wiki_mod._fallback_title(Path("x.md"), "# 标题\n正文")))
        emb = await call("memory.embed", memory_mod.Memory().embed, ["a"])
        check("memory.embed 结构", isinstance(emb, list) and emb and emb[0][:3] == VEC, str(emb)[:36])
        mem = core.memory
        check("memory._all_vectors", isinstance(mem._all_vectors(), list))
        check("memory.forgotten_important", isinstance(mem.forgotten_important(), list))
        mem.add_document("体检记忆", "private:10001", 0.9, ["体检"], ["事实"], vec=VEC)
        await call("memory.ensure_vectors", mem.ensure_vectors)

        # ---------- files ----------
        check("files._safe_name", files_mod._safe_name("../../etc/passwd") != "../../etc/passwd")
        img = TMP / "pic.png"
        Image.new("RGB", (64, 64), (10, 20, 30)).save(img)
        data_url = "data:image/png;base64," + base64.b64encode(img.read_bytes()).decode()

        class FakeBot:
            async def call_api(self, *a, **kw):
                return {"url": data_url}

        seg = SimpleNamespace(data={"file": "x.png", "url": "https://x/pic.png"})
        p = await call("files.save_image", files_mod.save_image, FakeBot(), seg)
        check("files.save_image 落盘", bool(p) and Path(str(p)).exists(), str(p))
        p2, nm = await call("files.save_from_segment", files_mod.save_from_segment, FakeBot(),
                            SimpleNamespace(data={"file": "x.png", "url": "https://x/pic.png"}))
        check("files.save_from_segment", bool(p2), str(nm))

        # ---------- web ----------
        check("web.extract_text", "正文" in web_mod.extract_text(HTML)[1])
        await call("web.search", web_mod.search, "test")
        await call("web.fetch", web_mod.fetch, "https://example.com")
        await call("web.web_open", web_mod.web_open, core, "https://example.com")

        # ---------- chat 补充 ----------
        real = chat_mod.ChatCore()
        real.llm.key_fn = lambda: "sk-test"
        await real.init_db()
        check("chat.usage_stats", isinstance(await real.usage_stats(1), dict))
        check("chat.reload_persona", (real.reload_persona() or True) is True)
        await call("chat.complete", real.complete, [{"role": "user", "content": "x"}])

        # ---------- scheduler ----------
        sent = []

        async def fake_send(text):
            sent.append(text)
            return True

        sch = sched_mod.Scheduler(core, send=fake_send)
        await call("scheduler._safe", sch._safe, "x", asyncio.sleep(0))
        sch._log_cleanup({"archived": 1, "alive": 2, "details": []})
        check("scheduler._log_cleanup", (TMP / "cleanup.log").exists())
        for m in ("_run_cleanup", "_reindex", "_run_followup_expire", "_run_weekly",
                  "_run_kb_audit", "_run_mood_snapshot", "_run_raw_archive",
                  "_run_agent_inbox"):
            await call(f"scheduler.{m}", getattr(sch, m))
        await call("scheduler._run_job_audit", sch._run_job_audit, day)
        for job in ("diary", "mood-snapshot", "review", "spaced", "raw-archive",
                    "cleanup", "followup-expire", "job-audit", "agent-inbox"):
            await call(f"scheduler._run_job({job})", sch._run_job, job, day)
        await call("scheduler._reminder_text", sch._reminder_text, "交周报")
        remind_mod.add("喝水", "2030-01-01 09:00")
        await call("scheduler._run_reminders", sch._run_reminders)
        check("scheduler.missing_jobs", isinstance(await sch.missing_jobs(day), list))
        sch.start()
        await asyncio.sleep(0.3)
        await sch.stop()


def fmt() -> bool:
    ok = sum(1 for _, good, _ in RESULTS if good)
    print(f"\n=== 全链路体检：{ok}/{len(RESULTS)} 通过 ===")
    bad = [r for r in RESULTS if not r[1]]
    if bad:
        print("\n失败项：")
        for name, _, extra in bad:
            print(f"  - {name}: {extra}")
    return ok == len(RESULTS)


try:
    asyncio.run(main())
finally:
    # 体检用的临时目录同样跑完就删，别在仓库根目录攒 smoke-*
    import shutil

    shutil.rmtree(TMP, ignore_errors=True)
sys.exit(0 if fmt() else 1)
