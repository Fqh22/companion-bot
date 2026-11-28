#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""批次 3 护栏自检：主体导出往返 / 面板可观测 / 分层预算 / 只读性 / 配置。

不依赖第三方库、不碰生产数据；除了正例还注入反例，确认守门员会红。
本地 Python 3.13 缺 numpy / yaml，所以这里只 import 纯 stdlib 的模块，
`memory_store.touch` 的行为由云端离线回归覆盖，本脚本做静态防回归。
"""
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CODE = Path(os.environ.get("QQBOT_CODE_DIR", str(ROOT / "nonebot"))).resolve()
sys.path.insert(0, str(CODE))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

from app_core import prompt  # noqa: E402

TOTAL = 0
FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    global TOTAL
    TOTAL += 1
    print(("PASS " if cond else "FAIL ") + name + (f" | {extra}" if extra else ""))
    if not cond:
        FAILS.append(name)


def load_ops(mod_name: str, filename: str):
    spec = importlib.util.spec_from_file_location(mod_name, ROOT / "ops" / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def make_sources(root: Path) -> dict[str, Path]:
    """造一份最小但结构完整的「主体」，含向量 blob 用于验证不被导出。"""
    root.mkdir(parents=True, exist_ok=True)
    persona = root / "persona.json"
    persona.write_text(json.dumps({"system_prompt": "你是它。",
                                   "begin_dialogs": ["在吗", "在"]},
                                  ensure_ascii=False), encoding="utf-8")
    mem_db = root / "livingmemory.db"
    c = sqlite3.connect(mem_db)
    c.execute("CREATE TABLE documents(id INTEGER PRIMARY KEY, doc_id TEXT, "
              "text TEXT, metadata TEXT)")
    c.execute("INSERT INTO documents VALUES(1,'doc-1','他在准备面试',"
              "'{\"status\":\"active\"}')")
    c.execute("CREATE TABLE memory_atoms(id INTEGER PRIMARY KEY, parent_memory_id INT, "
              "content TEXT, vec BLOB, status TEXT)")
    c.execute("INSERT INTO memory_atoms VALUES(1,1,'他在准备面试',X'01020304','active')")
    c.execute("CREATE TABLE memory_vectors(doc_id TEXT, vec BLOB)")
    c.commit()
    c.close()
    chat_db = root / "chat.db"
    c = sqlite3.connect(chat_db)
    c.execute("CREATE TABLE followups(id INTEGER PRIMARY KEY, text TEXT, done INT)")
    c.execute("INSERT INTO followups VALUES(1,'问他面试结果',0)")
    c.commit()
    c.close()
    profile = root / "profile.json"
    profile.write_text(json.dumps({"审美与偏好": {"v": "简洁"}}, ensure_ascii=False),
                       encoding="utf-8")
    rel = root / "relation.md"
    rel.write_text("# 关系\n认识三个月。\n", encoding="utf-8")
    corr = root / "corrections.md"
    corr.write_text("- 别叫我全名\n", encoding="utf-8")
    return {"persona": persona, "mem_db": mem_db, "chat_db": chat_db,
            "profile": profile, "relations": rel, "corrections": corr}


def export_checks() -> None:
    es = load_ops("export_subject", "export-subject.py")
    with tempfile.TemporaryDirectory(prefix="subject-guard-") as td:
        root = Path(td)
        src = make_sources(root / "src")
        parts, report = es.collect(src, now=1700000000.0)
        out = root / "out"
        out.mkdir()
        for name, blob in parts.items():
            (out / name).write_bytes(blob)

        ok, errors = es.verify(out)
        check("主体导出：往返校验通过", ok, "; ".join(errors))
        manifest = json.loads(parts["manifest.json"].decode("utf-8"))
        check("主体导出：manifest 有版本 / 分片 / 条目数",
              manifest.get("format_version") == es.FORMAT_VERSION
              and len(manifest.get("parts") or {}) == len(parts) - 1
              and manifest["counts"]["documents"] == 1)
        check("主体导出：README 说明格式与不导出的东西",
              b"format_version" in parts["README.md"]
              and b"keys.json" in parts["README.md"])
        mem_lines = parts["memory.jsonl"].decode("utf-8").splitlines()
        rows = [json.loads(x) for x in mem_lines]
        check("主体导出：记忆每行带 _table 且 doc_id 在",
              len(rows) == 2 and all("_table" in r for r in rows)
              and rows[0]["doc_id"] == "doc-1")
        check("主体导出：向量 blob 不进快照（换引擎要重建）",
              rows[1].get("vec") is None)
        check("主体导出：JSON 排序缩进、可 diff",
              '"begin_dialogs"' in parts["persona.json"].decode("utf-8")
              and parts["persona.json"].decode("utf-8").index('"begin_dialogs"')
              < parts["persona.json"].decode("utf-8").index('"system_prompt"'))
        check("主体导出：跟进 / 关系 / 纠正都在",
              "问他面试结果".encode() in parts["followups.json"]
              and "认识三个月".encode() in parts["relations.md"]
              and "别叫我全名".encode() in parts["corrections.md"])
        # 反例：篡改一个字节 / 删一个分片，都要被抓到
        (out / "persona.json").write_bytes(parts["persona.json"] + b" ")
        ok2, _ = es.verify(out)
        check("主体导出：篡改文件会被校验抓到", not ok2)
        (out / "persona.json").write_bytes(parts["persona.json"])
        (out / "relations.md").unlink()
        ok3, _ = es.verify(out)
        check("主体导出：缺分片会被校验抓到", not ok3)

        # 反例：损坏的 SQLite 源不能产出「校验通过的空快照」。
        bad_src = make_sources(root / "bad")
        bad_src["mem_db"].write_bytes(b"this is not a sqlite database")
        parts_b, report_b = es.collect(bad_src, now=1700000000.0)
        manifest_b = json.loads(parts_b["manifest.json"].decode("utf-8"))
        check("主体导出：损坏的 SQLite 源记为 failed",
              report_b["sources"].get("mem_db") == "failed"
              and bool(manifest_b.get("errors")), str(report_b["sources"]))
        out_b = root / "out_bad"
        out_b.mkdir()
        for name, blob in parts_b.items():
            (out_b / name).write_bytes(blob)
        ok_b, _ = es.verify(out_b)
        check("主体导出：损坏源生成的快照校验不通过", not ok_b)

        # 空环境：所有源都缺 → 允许（missing），导出仍可校验通过。
        empty = {k: root / "empty" / f"{k}.missing" for k in src}
        parts_e, report_e = es.collect(empty, now=1700000000.0)
        out_e = root / "out_empty"
        out_e.mkdir()
        for name, blob in parts_e.items():
            (out_e / name).write_bytes(blob)
        ok_e, err_e = es.verify(out_e)
        check("主体导出：空环境（全 missing）可导出且校验通过",
              ok_e and not report_e["errors"]
              and all(v == "missing" for v in report_e["sources"].values()),
              "; ".join(err_e))


def budget_checks() -> None:
    mem = [
        {"text": "洞察一", "type": "insight"},
        {"text": "洞察二", "type": "insight"},
        {"text": "事实一", "type": "fact"},
        {"text": "事实二", "type": "fact"},
    ]
    legacy = prompt._compact_background(mem, "", [], "", "")
    legacy_default = prompt._compact_background(mem, "", [], "", "", "legacy")
    check("分层预算：legacy 与不传参逐字节相同", legacy == legacy_default)
    check("分层预算：legacy 只取前 3 条（事实被挤掉一条）",
          "事实二" not in legacy)
    tiered = prompt._compact_background(mem, "", [], "", "", "tiered")
    check("分层预算：tiered 给 insight 独立配额，事实不再被挤掉",
          "事实一" in tiered and "事实二" in tiered
          and "洞察一" in tiered, tiered[:80].replace("\n", " / "))
    cfg = (CODE / "app_core" / "config.py").read_text(encoding="utf-8")
    ex = (CODE / "config.example.yaml").read_text(encoding="utf-8")
    check("分层预算：默认 legacy，与示例配置同步",
          '"budget_mode": "legacy"' in cfg
          and 'budget_mode: "legacy"' in ex)


def decay_and_consolidation_checks() -> None:
    ms = (CODE / "app_core" / "memory_store.py").read_text(encoding="utf-8")
    check("访问强化：touch 真的写 reinforcement_count",
          "reinforcement_count=reinforcement_count+1" in ms
          and "last_reinforced_at=?" in ms)
    check("访问强化：仍然只记账、不改状态",
          "UPDATE memory_atoms SET" in ms and "DELETE FROM documents" not in ms.split("def touch")[1].split("def ")[0])
    check("已停止采集碎片候选", "_shadow_dedup" not in ms)

    cons = load_ops("consolidate_mod", "consolidate.py")
    with tempfile.TemporaryDirectory(prefix="consolidate-guard-") as td:
        log = Path(td) / "consolidation-candidates.jsonl"
        recs = [
            {"cos": 0.93, "new": "他在准备面试", "old_id": "aaa", "old_text": "他在准备面试"},
            {"cos": 0.91, "new": "他在准备面试", "old_id": "aaa", "old_text": "他在准备面试"},
            {"cos": 0.86, "new": "他喜欢美式", "old_id": "bbb", "old_text": "他爱喝美式"},
            {"cos": 0.40, "new": "无关", "old_id": "ccc", "old_text": "别的"},
        ]
        log.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in recs) + "\n",
                       encoding="utf-8")
        r = cons.report(log)
        check("碎片候选：去重与分档正确",
              r["samples"] == 4 and r["pairs"] == 3 and r["high"] == 1
              and r["mid"] == 1 and r["low"] == 1, str(r["pairs"]))
        check("碎片候选：识别同文重复（最安全的一类）", r["same_text"] == 1)
    src = (ROOT / "ops" / "consolidate.py").read_text(encoding="utf-8")
    check("碎片整合脚本只读，不写库",
          "UPDATE documents" not in src and "INSERT INTO" not in src
          and "DELETE FROM" not in src)


def main() -> None:
    export_checks()
    budget_checks()
    decay_and_consolidation_checks()
    if FAILS:
        print(f"\n=== {TOTAL - len(FAILS)}/{TOTAL} 通过；失败：{', '.join(FAILS)} ===")
        sys.exit(1)
    print(f"\n=== {TOTAL}/{TOTAL} 通过 ===")


if __name__ == "__main__":
    main()
