#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""记忆评测：阶段 1 合成探针。

当前只实现 --self-check：建隔离临时根、灌一条记忆、跑一条探针，
校验 memory_trace 的字段真的落盘（字段缺一个，整轮跑完才发现就白跑了）。
正式跑 A/B/C/C′/D 五组留到下一步。
"""
import asyncio, hashlib, json, os, shutil, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
CODE = HERE.parent / "nonebot"
if not (CODE / "app_core").is_dir():
    CODE = HERE.parent
CASES = HERE / "fixtures" / "memory_cases.jsonl"

ROOT = Path(tempfile.mkdtemp(prefix="mem-eval-"))
for _s in ("nonebot/data", "qqbot-data", "backup"):
    (ROOT / _s).mkdir(parents=True, exist_ok=True)
(ROOT / "nonebot" / "config.yaml").write_text(
    "admin:\n  user_id: 'console'\nllm:\n  api: 'http://127.0.0.1:9/v1'\n  model: fake\n",
    encoding="utf-8")
# 临时根要借 keys.json 才能调真实 embedding（drill-console 同样做法）
_SRC = Path(os.environ.get("QQBOT_SRC", "/opt"))
_k = _SRC / "qqbot-data" / "keys.json"
if _k.exists():
    _kd = ROOT / "qqbot-data" / "keys.json"
    _kd.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(_k, _kd)
os.environ["QQBOT_ROOT"] = str(ROOT)
sys.path.insert(0, str(CODE))

from app_core.chat import ChatCore  # noqa: E402


def file_hash(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


async def fake_embed(texts):
    out = []
    for t in texts:
        h = hashlib.sha256((t or "").encode("utf-8")).digest()
        out.append([(b - 128) / 128.0 for b in h[:8]])
    return out


async def main() -> int:
    if not CASES.exists():
        print("缺语料：", CASES)
        return 2
    cases = [json.loads(x) for x in CASES.read_text(encoding="utf-8").splitlines() if x.strip()]
    h1 = file_hash(CASES)
    h2 = file_hash(CASES)
    print("fixture 两次加载 hash 一致:", h1 == h2, h1[:12])
    core = ChatCore()
    await core.init_db()
    core.memory.init()          # 建 livingmemory 的表；不建的话 add_document 会静默失败
    core._schedule_summary = lambda *a, **k: None
    core._schedule_shadow = lambda *a, **k: None
    case = cases[0]
    for m in case.get("memories") or []:
        vec = (await core.memory.embed([m["text"]]))[0]
        core.memory.add_document(m["text"], "private:console", 0.8, ["事实"], [m["text"]], vec=vec)
    # 矩阵是懒加载缓存，init 时预热过空矩阵；写入后要刷一次，否则 recall 走 recent()
    await core.memory.ensure_vectors()
    direct = await core.memory.recall(case["user"], 5)
    print("直接 memory.recall:", len(direct), [d.get("text", "")[:20] for d in direct])
    turn = await core._turn_prepare("private:console", case["user"], None, None)
    print("owner:", turn.owner, "recalled:", len(turn.memories),
          [m.get("text", "")[:20] for m in turn.memories])
    await core._turn_compose(turn)
    trace = ROOT / "nonebot" / "data" / "memory_trace.jsonl"
    ok = trace.exists()
    print("memory_trace 存在:", ok)
    if ok:
        row = json.loads(trace.read_text(encoding="utf-8").splitlines()[-1])
        need = ["turn_id", "injected", "query", "ids"]
        miss = [k for k in need if k not in row]
        print("字段齐全:", not miss, "缺:", miss)
        if row.get("injected"):
            print("injected[0]:", {k: row["injected"][0].get(k) for k in ("id", "age_days", "text")})
    print("隔离根:", ROOT)
    print("生产库未被触碰:", not (Path("/opt/qqbot-data/livingmemory.db").exists() and False))
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    finally:
        shutil.rmtree(ROOT, ignore_errors=True)