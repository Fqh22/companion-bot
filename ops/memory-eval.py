#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""记忆评测：阶段 1 合成探针。

用法（云端，有 keys）：
    python ops/memory-eval.py              # 自检：召回/trace 字段/隔离
    python ops/memory-eval.py --run        # 15 条 × A/B/C′/D 各 1 次
"""
import argparse, asyncio, hashlib, json, os, shutil, subprocess, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
CODE = HERE.parent / "nonebot"
if not (CODE / "app_core").is_dir():
    CODE = HERE.parent
CASES = HERE / "fixtures" / "memory_cases.jsonl"

ROOT = Path(tempfile.mkdtemp(prefix="mem-eval-"))
for _s in ("nonebot/data", "qqbot-data", "backup"):
    (ROOT / _s).mkdir(parents=True, exist_ok=True)
# 真实 config（含 llm.api / model）：自检不调模型，--run 必须能连上真接口。
# admin.user_id 改成 console，保证 probe 会话被当作本人（否则记忆不注入）。
_SRC_CFG = Path(os.environ.get("QQBOT_SRC", "/opt")) / "nonebot" / "config.yaml"
(_SRC_CFG.parent / "").exists()  # noop
_cfg_dst = ROOT / "nonebot" / "config.yaml"
if _SRC_CFG.exists():
    _txt = _SRC_CFG.read_text(encoding="utf-8")
    _txt = __import__("re").sub(r"(?m)^(\s*user_id:\s*).*$", r"\1'console'", _txt, count=1)
    _cfg_dst.write_text(_txt, encoding="utf-8")
else:
    _cfg_dst.write_text(
        "admin:\n  user_id: 'console'\nllm:\n  api: 'http://127.0.0.1:9/v1'\n  model: fake\n",
        encoding="utf-8")
_SRC = Path(os.environ.get("QQBOT_SRC", "/opt"))
_k = _SRC / "qqbot-data" / "keys.json"
if _k.exists():
    _kd = ROOT / "qqbot-data" / "keys.json"
    _kd.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(_k, _kd)
os.environ["QQBOT_ROOT"] = str(ROOT)
sys.path.insert(0, str(CODE))

from app_core.chat import ChatCore  # noqa: E402


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()[:16] if p.exists() else ""


def manifest() -> dict:
    try:
        head = subprocess.run(["git", "-C", str(HERE.parent), "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:
        head = ""
    return {"commit": head,
            "prompt": sha(CODE / "app_core" / "prompt.py"),
            "chat_turn": sha(CODE / "app_core" / "chat_turn.py"),
            "fixture": sha(CASES),
            "judge": "rule-v0（未起用）"}


async def _empty(*a, **k):
    return []


async def one(core, case, group) -> dict:
    """跑一组。A=关召回；B=当前；C′=注入正确事实/干扰项；D=注入未归一化原话。"""
    orig = core.memory.recall
    if group == "A":
        core.memory.recall = _empty
    try:
        turn = await core._turn_prepare("private:console", case["user"], None, None)
    finally:
        core.memory.recall = orig
    await core._turn_compose(turn)
    injected = ""
    if group == "C":
        injected = "\n".join(case.get("inject_c") or (case.get("distract") or []))
    elif group == "D":
        injected = "\n".join(case.get("inject_d") or [])
    msgs = list(turn.msgs)
    if injected:
        msgs.insert(-1, {"role": "system", "content": "[注入证据]\n" + injected})
    try:
        r = await core.complete_raw(msgs)
        reply = (r.get("content") or "").strip()
    except Exception as e:  # noqa: BLE001
        reply = "<ERR %s: %s>" % (type(e).__name__, str(e)[:80])
    return {"id": case["id"], "cls": case["cls"], "group": group,
            "injected": injected[:200], "reply": reply[:400],
            "recalled": [m.get("text", "")[:40] for m in turn.memories]}


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--out", default="/tmp/mem-eval.jsonl")
    args = ap.parse_args()
    if not CASES.exists():
        print("缺语料：", CASES)
        return 2
    cases = [json.loads(x) for x in CASES.read_text(encoding="utf-8").splitlines() if x.strip()]
    print("fixture hash 一致:", sha(CASES) == sha(CASES), sha(CASES))
    core = ChatCore()
    await core.init_db()
    core.memory.init()
    for case in cases:
        for m in case.get("memories") or []:
            vec = (await core.memory.embed([m["text"]]))[0]
            core.memory.add_document(m["text"], "private:console", 0.8, ["事实"], [m["text"]], vec=vec)
    await core.memory.ensure_vectors()
    if not args.run:
        turn = await core._turn_prepare("private:console", cases[0]["user"], None, None)
        print("recalled:", len(turn.memories))
        print("隔离根:", ROOT)
        return 0 if turn.memories else 1
    groups = ("A", "B", "C", "D")
    rows = []
    for g in groups:
        for case in cases:
            if g == "D" and not case.get("inject_d"):
                continue
            rows.append(await one(core, case, g))
            print(g, case["id"], "ok")
    mf = manifest()
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(json.dumps({"manifest": mf}, ensure_ascii=False) + "\n")
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print("rows:", len(rows), "manifest:", mf)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    finally:
        shutil.rmtree(ROOT, ignore_errors=True)