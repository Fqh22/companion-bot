#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""prompt 快照基线 —— 改造前后「同一份输入必须产出逐字节相同的 messages」。

用法：
    python ops/test-prompt-golden.py --self-check     # 冻结桩是否稳定（同 case 连跑两次）
    python ops/test-prompt-golden.py --write          # 在**旧代码**上生成基线
    python ops/test-prompt-golden.py                  # 与基线比对（CI 用）
    python ops/test-prompt-golden.py --mutate         # 故意打乱，确认会红

设计要点见 临时/companion-方案0-重做-turn-prepare-v3.md §5。
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time as _time
from pathlib import Path

HERE = Path(__file__).resolve().parent
CODE = HERE.parent / "nonebot"
if not (CODE / "app_core").is_dir():
    CODE = HERE.parent

# 数据根：尊重外部 QQBOT_ROOT（CI 会显式设 mktemp -d），但**只认临时目录**——
# 防止有人在生产根上跑 --write，把基线生成变成对真实数据的写入。
def _root_allowed(p: Path) -> bool:
    """数据根必须在临时目录下：防止在生产根上跑 --write。"""
    tmp = Path(tempfile.gettempdir()).resolve()
    try:
        rp = p.resolve()
    except Exception:
        return False
    return tmp == rp or tmp in rp.parents


_env_root = os.environ.get("QQBOT_ROOT")
if _env_root:
    ROOT = Path(_env_root)
    _allowed = _root_allowed(ROOT)
    if os.environ.get("QQBOT_ROOT_CHECK_ONLY"):
        # 只跑校验就退出：测试这条路径时不该建任何目录
        raise SystemExit(0 if _allowed else 3)
    if not _allowed:
        print("FATAL QQBOT_ROOT 不在临时目录下，拒绝运行：" + str(ROOT))
        raise SystemExit(3)
else:
    ROOT = Path(tempfile.mkdtemp(prefix="prompt-golden-"))
for _sub in ("nonebot/data", "qqbot-data", "backup"):
    (ROOT / _sub).mkdir(parents=True, exist_ok=True)

(ROOT / "nonebot" / "config.yaml").write_text(
    "admin:\n"
    "  user_id: 'console'\n"
    "llm:\n"
    "  api: 'http://127.0.0.1:9/v1/chat/completions'\n"
    "  model: 'fake'\n"
    "  temperature: 1.0\n"
    "  reply_deadline_seconds: 45\n"
    "summary:\n"
    "  model: 'fake-sum'\n"
    "chat:\n"
    "  share_tier: false\n"
    "  shadow_followup: false\n"
    "  shadow_sample: 0.0\n"
    "context:\n"
    "  history_max_chars: 6000\n"
    "  history_max_items: 40\n",
    encoding="utf-8")

os.environ["QQBOT_ROOT"] = str(ROOT)
sys.path.insert(0, str(CODE))

# ---- 冻结桩（独立模块，manifest 要能 hash 它）----
sys.path.insert(0, str(HERE / "fixtures"))
import freeze  # noqa: E402
import seed as seed_mod  # noqa: E402

freeze.install()

from app_core import perception, web  # noqa: E402
from app_core.chat import ChatCore, prompt_blocks  # noqa: E402


class FakeLLM:
    def __init__(self, text="嗯"):
        self.text = text

    async def complete_raw(self, *a, **k):
        return {"content": self.text, "tool_calls": [], "reasoning_content": ""}

    async def complete(self, *a, **k):
        return {"content": self.text, "tool_calls": [], "reasoning_content": ""}

    async def stream(self, messages, tools_arg=None, on_text=None, **kw):
        if on_text:
            on_text(self.text)
        return {"content": self.text, "tool_calls": [], "reasoning_content": ""}

    async def summarize(self, *a, **k):
        return "{}"


async def fake_embed(texts):
    """和 seed 写库时用同一份向量生成器，否则召回打不亮。"""
    return seed_mod.fake_embed(texts)


_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAE"
    "hQGAhKmMIQAAAABJRU5ErkJggg==")


def _materialize_images(case):
    n = case.get("images") or 0
    if isinstance(n, list):
        n = len(n)
    out = []
    for i in range(int(n)):
        d = ROOT / "img"
        d.mkdir(parents=True, exist_ok=True)
        f = d / f"a{i}.png"
        f.write_bytes(_PNG)
        out.append(str(f))
    return out


# 固定值注入，**不是**删文件走代码默认值：
# 删文件会让基线悄悄依赖 mood_domain.DEFAULT，而那个常量不在 manifest 里，
# 改一次默认值基线就崩、还难查。这里每个字段都显式写死。
FIXED_MOOD = {"valence": 0.12, "valence_base": 0.10, "energy": 0.70,
              "social": 0.60, "why": []}


def _reset_root():
    """每条 case 前**删掉并重建**数据根，而不是只在进程启动时 seed 一次。

    踩过的坑：只在启动时 seed 一次，5 条 case 顺序跑会累积状态
    （observe 写 mood、_log_prompt 插 prompt_log、trace/shadow 滚动写）。
    基线生成和比对都按同一顺序跑，sha 依然全绿——绿的是「共享同一套污染顺序」，
    不是「渲染逻辑稳定」。这条不修，等 S1a 后顺序一变就会红，且极难归因。
    """
    for sub in ("nonebot/data", "qqbot-data", "backup"):
        d = ROOT / sub
        shutil.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True, exist_ok=True)


def _seed_state(case):
    """情绪状态固定值注入（case 可覆盖 mood 的三值）。

    只写 mood：画像 / 关系 / 笔记 / 配置由 seed.write_files 负责，
    库里的记忆 / followup / 索引由 seed.build_db 负责。职责分开，
    谁改坏了容易定位。
    """
    mood = dict(FIXED_MOOD)
    mood.update(case.get("mood") or {})
    mood_dir = ROOT / "nonebot" / "data" / "plugin_data" / "mood"
    mood_dir.mkdir(parents=True, exist_ok=True)
    (mood_dir / "state.json").write_text(
        json.dumps(mood, ensure_ascii=False, indent=1), encoding="utf-8")


def _stub_weather(case):
    w = case.get("weather")
    if w is None:
        async def _none():
            return None
        perception.read = _none
    elif w == "missing":
        async def _miss():
            return None
        perception.read = _miss
    else:
        async def _ok():
            return {"text": w, "as_of": freeze.FIXED_TS}
        perception.read = _ok


def _stub_search(case):
    s = case.get("search")
    if case.get("search_error"):
        async def _boom(q, n):
            raise RuntimeError("stub: search down")
        web.search = _boom
        return
    if s is None:
        async def _none(q, n):
            return ""
        web.search = _none
    else:
        async def _hit(q, n):
            return s
        web.search = _hit


def _manifest(baseline_path: Path) -> dict:
    """基线清单：护住「工具 + 冻结桩 + seed + 语料」不被悄悄放宽。"""
    import platform
    import subprocess
    rels = ["ops/test-prompt-golden.py", "ops/fixtures/freeze.py",
            "ops/fixtures/seed.py", "ops/fixtures/prompt-cases.jsonl"]
    files = {}
    for rel in rels:
        q = HERE.parent / rel
        files[rel] = hashlib.sha256(q.read_bytes()).hexdigest() if q.exists() else "MISSING"
    files["ops/fixtures/prompt-baseline.json"] = hashlib.sha256(baseline_path.read_bytes()).hexdigest()
    try:
        head = subprocess.run(["git", "-C", str(HERE.parent), "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:
        head = ""
    return {"files": files, "git_head": head, "python": platform.python_version(),
            "metadata": {"platform": platform.system()}}


def _check_manifest(base_dir: Path) -> list[str]:
    """启动自检：清单里任何一份文件变了、或 Python 版本不一致，就拒跑（不是 WARN）。"""
    import platform
    mf = base_dir / "_manifest.sha256"
    if not mf.exists():
        return []
    try:
        data = json.loads(mf.read_text(encoding="utf-8"))
    except Exception:
        return ["_manifest.sha256 读不动"]
    bad = []
    # 只比 major.minor：patch 版本不影响 json.dumps 的 sha，但 3.12.3 与 3.12.14
    # 在云端/CI 之间必然不同——比完整版本等于把基线绑死在一台机器上。
    want_py = str(data.get("python") or "")
    cur_py = platform.python_version()
    if want_py and want_py.split(".")[:2] != cur_py.split(".")[:2]:
        bad.append(f"Python {want_py} != {cur_py}（只比 major.minor）")
    for rel, want in (data.get("files") or {}).items():
        q = HERE.parent / rel
        if not q.exists():
            bad.append(rel + " 缺失")
            continue
        got = hashlib.sha256(q.read_bytes()).hexdigest()
        if got != want:
            bad.append(rel + " 变了")
    return bad


async def _compare_all(baseline_path: Path) -> int:
    """跑全部 case 与基线比，返回失败条数（--mutate 用）。"""
    base = json.loads(baseline_path.read_text(encoding="utf-8"))
    fail = 0
    for case in CASES:
        got = summarize(await run_case(case))["sha"]
        if (base.get(case["id"]) or {}).get("sha") != got:
            fail += 1
    return fail


def msg_sha(msgs):
    return hashlib.sha256(
        json.dumps(msgs, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


async def run_case(case):
    _reset_root()
    _seed_state(case)
    seed_mod.write_files(ROOT, case)
    # config.load 有模块级缓存，不失效的话 per-case 的 share_tier / style_mode
    # 全部不会生效——5 条语料里 4 条 sha 雷同就是这么来的。
    from app_core import config as _cfg
    _cfg.load(force=True)
    core = ChatCore()
    await core.init_db()
    # 不能用 or 兜底：空串是「有图但没注」这一分支的输入，被吞掉就永远测不到。
    core.llm = FakeLLM(case["fake_llm"] if "fake_llm" in case else "嗯")
    # complete_raw 会先 _sync_llm_db()，可能把 core.llm 换回真 LLM——图注因此拿到空串。
    async def _fake_raw(*a, **k):
        return {"content": core.llm.text, "tool_calls": [], "reasoning_content": ""}
    core.complete_raw = _fake_raw
    core.memory.embed = fake_embed
    core._schedule_summary = lambda *a, **k: None
    core._schedule_shadow = lambda *a, **k: None
    await seed_mod.build_db(core, ROOT, case)
    sess = case.get("session", "private:console")
    # followup 的 done/confirm/drop 判定需要「上一轮 assistant 说过话」（followup_domain:85），
    # 没有历史时这些分支永远不触发。
    for h in case.get("history") or []:
        await core.remember(sess, h["role"], h["content"])
    _stub_weather(case)
    _stub_search(case)
    turn = await core._turn_prepare(sess, case["user"], _materialize_images(case), None)
    await core._turn_compose(turn)
    _assert_stubs(case, core)
    return turn.msgs


def _assert_stubs(case, core) -> None:
    """渲染**之后**再断言一次。

    _sync_llm_db() 是在 complete_raw 调用中途把 core.llm 换回真 LLM 的，
    只在 case 开始时断言 is FAKE 抓不到；config 缓存同理，可能在渲染中途被重新 load。
    """
    from app_core import config as _cfg
    want = bool((case.get("config") or {}).get("share_tier"))
    got = bool((_cfg.load().get("chat") or {}).get("share_tier"))
    if got != want:
        raise RuntimeError(f"桩失效：share_tier 期望 {want}、实际 {got}（{case['id']}）")
    if getattr(core.complete_raw, "__name__", "") != "_fake_raw":
        raise RuntimeError(f"桩失效：complete_raw 被换回真 LLM（{case['id']}）")


def summarize(msgs, with_raw=False):
    out = {
        "sha": msg_sha(msgs),
        "n": len(msgs),
        "chars": sum(len(m.get("content") or "") for m in msgs
                     if isinstance(m.get("content"), str)),
        "blocks": [{"role": b["role"], "chars": b["chars"], "label": b["label"]}
                   for b in prompt_blocks(msgs)],
    }
    if with_raw:
        out["raw"] = msgs
    return out


# 槽位 ← 块首行前缀。旧代码没有 slots，只能从渲染结果反推，
# 目的是让「哪一格空着」一眼可见，而不是感觉够了。
SLOT_MATCH = {
    "weather": ("【天气】",),
    "search": ("【本轮搜索",),
    "followup": ("他刚说", "他像是在说", "他刚把"),
    "style": ("【这次的形式】", "【这一轮】"),
    "reaction": ("（他发的是表情",),
    "caption": ("[图注]",),
    "share": ("【分享", "【这一轮的上限】"),
    "situation": ("[此刻]",),
    "relation": ("[我们之间]",),
}

# 9 槽 × 预设分支。每格带期望：nonempty = 必须有块，empty = 必须没有块。
# 只数「槽非空」会把降级路径全判成功——search 超时、caption 无注、share off、
# followup 都没有，正确结果本来就是块不存在。
MATRIX = {
    "weather":   {"hit": "nonempty", "fail": "nonempty", "none": "empty"},
    "search":    {"hit": "nonempty", "empty": "empty", "error": "empty", "none": "empty"},
    "followup":  {"done": "nonempty", "confirm": "nonempty", "drop": "nonempty", "none": "empty"},
    # model 模式不走 style_block（build_messages 用 MODEL_FIRST_* 承担），style 槽必须为空
    "style":     {"rules": "nonempty", "model": "empty"},
    "reaction":  {"hit": "nonempty", "none": "empty"},
    "caption":   {"note": "nonempty", "empty-note": "empty", "none": "empty"},
    "share":     {"t0": "nonempty", "t1": "nonempty", "t2": "nonempty",
                  "t3": "nonempty", "t4": "nonempty", "off": "empty"},
    "situation": {"hit": "nonempty"},
    "relation":  {"hit": "nonempty", "none": "empty"},
}


def slot_hits(blocks) -> dict:
    out = {}
    for b in blocks:
        for k, ps in SLOT_MATCH.items():
            if any(b["label"].startswith(x) for x in ps):
                out[k] = b["chars"]
    return out


def check_matrix(results):
    """results: [(case_id, slots, covers)]。返回 (missing, wrong)。"""
    cells = {(s, b): {"decl": 0, "bad": []}
             for s, bs in MATRIX.items() for b in bs}
    for cid, sl, covers in results:
        for item in covers or []:
            slot, branch, exp = item[0], item[1], item[2]
            cell = cells.setdefault((slot, branch), {"decl": 0, "bad": []})
            cell["decl"] += 1
            good = bool(sl.get(slot)) if exp == "nonempty" else not sl.get(slot)
            if not good:
                cell["bad"].append(cid)
    missing = [f"{s}/{b}" for s, bs in MATRIX.items() for b in bs
               if cells[(s, b)]["decl"] == 0]
    wrong = [f"{s}/{b}({','.join(cells[(s, b)]['bad'])})"
             for (s, b), v in cells.items() if v["bad"]]
    return missing, wrong


def matrix_report(results) -> bool:
    """缺格或期望不符都非零退出；不报平均覆盖率。"""
    missing, wrong = check_matrix(results)
    total = sum(len(b) for b in MATRIX.values())
    if not missing and not wrong:
        print(f"PASS 覆盖矩阵 {total} 格全部命中（每格至少一条 case 且期望相符）")
        return True
    if missing:
        print("FAIL 缺格：" + " ".join(missing))
    if wrong:
        print("FAIL 期望不符：" + " ".join(wrong))
    return False


_CASES_FILE = HERE / "fixtures" / "prompt-cases.jsonl"
if _CASES_FILE.exists():
    CASES = [json.loads(x) for x in
             _CASES_FILE.read_text(encoding="utf-8").splitlines() if x.strip()]
else:
    CASES = [
    {"id": "chat-only", "user": "在吗"},
    {"id": "weather-hit", "user": "今天北京天气怎么样", "weather": "北京 晴 18℃"},
    {"id": "weather-fail", "user": "今天上海天气怎么样", "weather": "missing"},
    {"id": "analytic-search", "user": "这个方案该不该做，帮我分析一下",
     "search": "1. 方案评估要点\n2. 成本收益"},
    {"id": "image-share", "user": "", "images": ["/tmp/a.jpg"]},
]


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-check", action="store_true")
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--mutate", action="store_true")
    ap.add_argument("--out", default="", help="基线写到别处（本地受沙箱限制时用）")
    ap.add_argument("--report", action="store_true", help="结构化摘要 + 槽位覆盖率")
    ap.add_argument("--min-cases", type=int, default=36)
    ap.add_argument("--offline", action="store_true",
                    help="socket 层断网，验证基线不依赖网络")
    ap.add_argument("--selftest", action="store_true",
                    help="顺序无关 + 冻结桩生效 + 语料生效（三条负向断言）")
    ap.add_argument("--allow-partial", action="store_true",
                    help="语料不足最低条数时仍允许比对（本地调试用，CI 不许）")
    args = ap.parse_args()

    baseline_path = HERE / "fixtures" / "prompt-baseline.json"

    if args.offline:
        import socket

        def _deny(*a, **k):
            raise OSError("offline stub: 网络被禁用")
        socket.create_connection = _deny
        socket.socket.connect = _deny
        socket.socket.connect_ex = _deny

    bad = _check_manifest(HERE / "fixtures")
    if bad and not (args.write or args.report or args.selftest):
        print("FATAL 基线清单对不上：" + "；".join(bad))
        print("     若确实要重建基线，跑 --write 覆盖清单。")
        return 4

    # 数据根隔离是 seed 的**前置条件**，不是收尾检查：
    # 若 paths.ROOT 指到生产目录，每生成一次基线都在改真实记忆/情绪/prompt_log。
    from app_core import paths as _paths
    if _paths.ROOT.resolve() != ROOT.resolve():
        print(f"FATAL 数据根未隔离：paths.ROOT={_paths.ROOT}，应为 {ROOT}")
        return 3

    if args.mutate:
        # 固化成 CI 自检：每次都必须「改了就红」，否则标尺本身已经失效。
        from dataclasses import replace

        from app_core import chat_turn as _ct

        origin = _ct.SLOTS
        ok = fail = 0

        def _ck(name, cond, extra=""):
            nonlocal ok, fail
            print(("PASS " if cond else "FAIL ") + name + ("  " + extra if extra else ""))
            ok, fail = (ok + 1, fail) if cond else (ok, fail + 1)

        try:
            base_fail = await _compare_all(baseline_path)
            _ck("未改动时基线全绿", base_fail == 0, f"fail={base_fail}")

            # M1 交换同轮共现的两个槽（caption 60 / share 70）
            _ct.SLOTS = tuple(
                replace(s, sid=(70 if s.key == "caption" else
                                60 if s.key == "share" else s.sid))
                for s in origin)
            n1 = await _compare_all(baseline_path)
            _ct.SLOTS = origin
            _ck("交换 caption/share 的 sid → 必须红", n1 > 0, f"fail={n1}")

            # M2 删掉一个槽 → 用到它的 case 必须红
            _ct.SLOTS = tuple(s for s in origin if s.key != "situation")
            n2 = await _compare_all(baseline_path)
            _ct.SLOTS = origin
            _ck("删掉 situation 槽 → 必须红", n2 > 0, f"fail={n2}")

            # M3 反序所有槽 → 顺序必须被看见
            _ct.SLOTS = tuple(replace(s, sid=100 - s.sid) for s in origin)
            n3 = await _compare_all(baseline_path)
            _ct.SLOTS = origin
            _ck("槽序整体反序 → 必须红", n3 > 0, f"fail={n3}")
        finally:
            _ct.SLOTS = origin

        n4 = await _compare_all(baseline_path)
        _ck("恢复后必须回到全绿", n4 == 0, f"fail={n4}")
        print(f"\n=== mutate 自检 {ok}/{ok + fail} 通过 ===")
        return 0 if fail == 0 else 1

    if args.selftest:
        ok = fail = 0

        def _ck(name, cond, extra=""):
            nonlocal ok, fail
            print(("PASS " if cond else "FAIL ") + name + ("  " + extra if extra else ""))
            ok, fail = (ok + 1, fail) if cond else (ok, fail + 1)

        # 1) 顺序无关：case 之间必须互不污染
        fwd = {}
        for c in CASES:
            fwd[c["id"]] = summarize(await run_case(c))["sha"]
        rev = {}
        for c in list(reversed(CASES)):
            rev[c["id"]] = summarize(await run_case(c))["sha"]
        diff = [k for k in fwd if fwd[k] != rev.get(k)]
        _ck("顺序无关（正序与倒序 sha 一致）", not diff,
            str(diff[:3]) if diff else f"{len(fwd)} 例")
        cache = fwd

        # 2) 冻结桩生效：改时钟，sha 必须变
        old_ts = freeze.FIXED_TS
        freeze.set_ts(old_ts + 3600)
        try:
            moved = summarize(await run_case(CASES[0]))["sha"]
        finally:
            freeze.set_ts(old_ts)
        _ck("冻结桩生效（改时钟 → sha 变）", moved != cache[CASES[0]["id"]])

        # 3) 语料生效：改输入，sha 必须变
        orig = CASES[0]["user"]
        try:
            CASES[0]["user"] = orig + "，再说点"
            altered = summarize(await run_case(CASES[0]))["sha"]
        finally:
            CASES[0]["user"] = orig
        _ck("语料生效（改输入 → sha 变）", altered != cache[CASES[0]["id"]])

        # 4) 数据根校验：纯函数 + 端到端（CHECK_ONLY：只跑校验就退出，不建目录，
        #    测试本身不可能写坏宿主；也不依赖「项目根是否恰好在 /tmp」这种巧合）
        import os as _os
        import subprocess as _sp
        _ck("校验函数拒绝非临时根", not _root_allowed(Path("/opt/not-a-root")))
        _ck("校验函数接受临时根", _root_allowed(ROOT))
        env = dict(_os.environ)
        env["QQBOT_ROOT"] = "/opt/not-a-root"
        env["QQBOT_ROOT_CHECK_ONLY"] = "1"
        pr = _sp.run([sys.executable, __file__], capture_output=True,
                     text=True, timeout=120, env=env)
        _ck("数据根未隔离 → exit 3", pr.returncode == 3, f"rc={pr.returncode}")

        print(f"\n=== selftest {ok}/{ok + fail} 通过 ===")
        return 0 if fail == 0 else 1

    if args.report:
        print("=== 数据根 ===")
        print("QQBOT_ROOT =", _paths.ROOT)
        print("DATA       =", _paths.DATA)
        print("=== 冻结桩 ===")
        print("time.time      -> 1794744000.0")
        print("time.localtime -> 同上")
        print("time.strftime  -> 同上")
        print("time.monotonic -> 不冻结（asyncio 超时依赖）")
        print("datetime.now   -> 未冻结（27 处，靠自检暴露）")
        print("mood 状态      -> 固定值注入", FIXED_MOOD)
        cover = {k: 0 for k in SLOT_MATCH}
        results = []
        print("\n=== 用例 ===")
        for case in CASES:
            sm = summarize(await run_case(case))
            sl = slot_hits(sm["blocks"])
            results.append((case["id"], sl, case.get("covers")))
            for k, v in sl.items():
                if v:
                    cover[k] += 1
            shas = {}
            for b in sm["blocks"]:
                for k, ps in SLOT_MATCH.items():
                    if any(b["label"].startswith(x) for x in ps):
                        shas[k] = hashlib.sha256(b["label"].encode()).hexdigest()[:8]
            hits = " ".join(f"{k}:{v}/{shas.get(k, '-')}" for k, v in sl.items() if v)
            print(f"{case['id']:16} {sm['sha'][:12]} {sm['n']:2d}段 {sm['chars']:5d}字  {hits or '(无非空槽)'}")
        print(f"\n=== 槽位覆盖（{len(SLOT_MATCH)} 槽）===")
        for k in SLOT_MATCH:
            print(f"  {k:10} {'有块' if cover[k] else '空'}  {cover[k]}/{len(CASES)} 例")
        filled = sum(1 for k in SLOT_MATCH if cover[k])
        print(f"\n有块槽位 {filled}/{len(SLOT_MATCH)}；语料 {len(CASES)} 条（最低 {args.min_cases}）")
        if len(CASES) < args.min_cases:
            print(f"WARN 语料不足 {len(CASES)}/{args.min_cases}：覆盖率报表不代表契约已验证")
        print()
        return 0 if matrix_report(results) else 2

    if len(CASES) < args.min_cases and not args.allow_partial:
        print(f"FAIL 语料不足 {len(CASES)}/{args.min_cases}："
              f"「全绿」不代表槽位契约已验证（--allow-partial 仅供本地调试）")
        return 2

    ok = fail = 0
    results = []

    for case in CASES:
        first = summarize(await run_case(case), with_raw=args.self_check)
        results.append((case["id"], slot_hits(first["blocks"]), case.get("covers")))
        if args.self_check:
            second = summarize(await run_case(case), with_raw=True)
            same = first["sha"] == second["sha"]
            print(("PASS " if same else "UNSTABLE ") + case["id"]
                  + (" | " + first["sha"][:12] if same
                     else f" | {first['sha'][:12]} vs {second['sha'][:12]}"))
            ok, fail = (ok + 1, fail) if same else (ok, fail + 1)
            if not same:
                for i, (a, b) in enumerate(zip(first["raw"], second["raw"])):
                    if a != b:
                        print("   首个差异 message", i)
                        print("   A:", json.dumps(a, ensure_ascii=False)[:300])
                        print("   B:", json.dumps(b, ensure_ascii=False)[:300])
                        break
                else:
                    print("   数量不同", len(first["raw"]), len(second["raw"]))
            continue

        if args.write:
            print(f"WRITE {case['id']} {first['sha'][:12]} ({first['n']} 段 / {first['chars']} 字)")
            continue

        base = json.loads(baseline_path.read_text(encoding="utf-8")) if baseline_path.exists() else {}
        want = (base.get(case["id"]) or {}).get("sha")
        if want is None:
            print("SKIP", case["id"], "基线里没有")
            continue
        if want == first["sha"]:
            ok += 1
            print(f"PASS {case['id']}")
        else:
            fail += 1
            print(f"FAIL {case['id']} {want[:12]} -> {first['sha'][:12]}")
            wb = (base.get(case["id"]) or {}).get("blocks") or []
            for i, (a, b) in enumerate(zip(wb, first["blocks"])):
                if a != b:
                    print("   首个差异块", i, a, "->", b)
                    break

    if args.write:
        data = {}
        results = []
        for case in CASES:
            sm = summarize(await run_case(case))
            data[case["id"]] = sm
            results.append((case["id"], slot_hits(sm["blocks"]), case.get("covers")))
        # 覆盖不全的语料不能生成「合法」基线
        if not matrix_report(results):
            return 2
        out = Path(args.out) if args.out else baseline_path
        out.parent.mkdir(parents=True, exist_ok=True)
        # newline="\n"：Windows 生成、Linux CI 消费，CRLF 会让 manifest 的 sha 对不上
        out.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                       encoding="utf-8", newline="\n")
        man = out.parent / "_manifest.sha256"
        man.write_text(json.dumps(_manifest(out), ensure_ascii=False, indent=2),
                       encoding="utf-8", newline="\n")
        print(f"\n=== 基线已写：{out}（{len(data)} 条）===")
        print(f"=== 清单已写：{man} ===")
        return 0

    if not matrix_report(results):
        return 2
    print(f"\n=== {ok}/{ok + fail} 通过 ===")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    try:
        rc = asyncio.run(main())
    finally:
        shutil.rmtree(ROOT, ignore_errors=True)
    raise SystemExit(rc)
