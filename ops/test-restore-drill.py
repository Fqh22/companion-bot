#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""恢复演练的护栏自检：缺库 / 空表 / 清单不符 / 旧备份都要分别判对。

用临时 tgz 造四种场景，直接调 ops/restore-drill.py 的 main()：
1) 有效清单 + 有数据        → 0
2) 缺 livingmemory.db        → 失败（以前只遍历现有库，缺库也能退出 0）
3) 清单说有 1 条、恢复后 0 条 → 失败（备份时有数据、恢复后为空）
4) 清单说本来就是 0 条       → 0，并标 empty_by_design（新装允许空库）
5) 没有 manifest 的旧备份 + 空表 → 失败（无法区分空与坏，按严格模式）
"""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import sqlite3
import sys
import tarfile
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
DRILL = HERE / "restore-drill.py"

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

TOTAL = 0
FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    global TOTAL
    TOTAL += 1
    print(("PASS " if cond else "FAIL ") + name + (f" | {extra}" if extra else ""))
    if not cond:
        FAILS.append(name)


def load_drill():
    spec = importlib.util.spec_from_file_location("restore_drill", DRILL)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _db_bytes(tables: dict[str, int]) -> bytes:
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        name = f.name
    try:
        c = sqlite3.connect(name)
        for table, rows in tables.items():
            c.execute(f"CREATE TABLE {table}(id INTEGER PRIMARY KEY, text TEXT)")
            for i in range(rows):
                c.execute(f"INSERT INTO {table}(text) VALUES(?)", (f"row-{i}",))
        c.commit()
        c.close()
        return Path(name).read_bytes()
    finally:
        Path(name).unlink(missing_ok=True)


def _bundle(path: Path, *, with_memory: bool, messages: int,
            manifest: bool, manifest_messages: int | None = None,
            with_adapters: bool = True) -> Path:
    files: dict[str, bytes] = {}
    files["sqlite/nonebot/data/chat.db"] = _db_bytes({"messages": messages})
    if with_memory:
        files["sqlite/qqbot-data/livingmemory.db"] = _db_bytes(
            {"documents": 1, "memory_atoms": 0})
    files["code/app_core/chat.py"] = b"# chat\n"
    files["code/plugins/chat/__init__.py"] = b"from adapters.qq import to_inbound\n"
    files["code/plugins/feishu/__init__.py"] = (
        b"from adapters.feishu import parse_content, to_inbound\n")
    if with_adapters:
        files["code/adapters/qq.py"] = b"def to_inbound(**kw):\n    return kw\n"
        files["code/adapters/feishu.py"] = (
            b"def to_inbound(**kw):\n    return kw\n"
            b"def parse_content(raw):\n    return {}\n")
    if manifest:
        dbs = {"sqlite/nonebot/data/chat.db": {
            "tables": {"messages": messages if manifest_messages is None
                       else manifest_messages}}}
        if with_memory:
            dbs["sqlite/qqbot-data/livingmemory.db"] = {
                "tables": {"documents": 1, "memory_atoms": 0}}
        files["manifest.json"] = json.dumps({
            "format": 2,
            "created_at": 1.0,
            "dbs": dbs,
            "files": {rel: {"sha256": hashlib.sha256(blob).hexdigest(),
                            "bytes": len(blob)}
                      for rel, blob in files.items()},
        }, ensure_ascii=False).encode("utf-8")
    with tarfile.open(path, "w:gz") as tf:
        for rel, blob in files.items():
            info = tarfile.TarInfo(rel)
            info.size = len(blob)
            info.mtime = 1700000000
            tf.addfile(info, io.BytesIO(blob))
    return path


def run_case(mod, td: Path, name: str, **kw) -> tuple[int, dict]:
    tgz = _bundle(td / f"{name}.tgz", **kw)
    mod.STATE = td / "restore.state"
    mod.LOG = td / "restore.log"
    old_argv = sys.argv
    sys.argv = ["restore-drill.py", str(tgz)]
    try:
        rc = mod.main()
    finally:
        sys.argv = old_argv
    state = json.loads(mod.STATE.read_text(encoding="utf-8"))
    return rc, state


def main() -> int:
    mod = load_drill()
    with tempfile.TemporaryDirectory(prefix="restore-drill-test-") as raw:
        td = Path(raw)
        rc, st = run_case(mod, td, "ok", with_memory=True, messages=3, manifest=True)
        check("有效备份 + 清单 → 通过", rc == 0 and st["ok"], str(st.get("errors")))

        rc, st = run_case(mod, td, "missing", with_memory=False, messages=3,
                          manifest=True)
        check("缺 livingmemory.db → 失败", rc != 0 and not st["ok"],
              str(st.get("errors"))[:120])

        rc, st = run_case(mod, td, "emptied", with_memory=True, messages=0,
                          manifest=True, manifest_messages=1)
        check("备份有数据、恢复后为空 → 失败", rc != 0 and not st["ok"],
              str(st.get("errors"))[:120])

        rc, st = run_case(mod, td, "empty-ok", with_memory=True, messages=0,
                          manifest=True)
        check("备份时就是空库 → 通过且标 empty_by_design",
              rc == 0 and st["ok"]
              and any(d.get("empty_by_design") for d in st["dbs"]),
              str([(d.get("rows"), d.get("empty_by_design")) for d in st["dbs"]]))

        rc, st = run_case(mod, td, "legacy", with_memory=True, messages=0,
                          manifest=False)
        check("旧备份（无清单）+ 空表 → 失败", rc != 0 and not st["ok"],
              str(st.get("errors"))[:120])

        rc, st = run_case(mod, td, "no-adapters", with_memory=True, messages=3,
                          manifest=True, with_adapters=False)
        check("缺 adapters → 失败",
              rc != 0 and not st["ok"] and any("adapters" in e for e in st["errors"]),
              str(st.get("errors"))[:160])

        # 压缩包被截断：连解压都不该通过
        tgz = _bundle(td / "tampered.tgz", with_memory=True, messages=3, manifest=True)
        tgz.write_bytes(tgz.read_bytes()[:-40])
        mod.STATE = td / "restore.state"
        mod.LOG = td / "restore.log"
        old_argv = sys.argv
        sys.argv = ["restore-drill.py", str(tgz)]
        try:
            rc = mod.main()
        finally:
            sys.argv = old_argv
        check("损坏的压缩包 → 失败", rc != 0)

    print(f"\n=== {TOTAL - len(FAILS)}/{TOTAL} 通过 ===" if not FAILS
          else f"\n=== 失败：{', '.join(FAILS)} ===")
    return 0 if not FAILS else 1


if __name__ == "__main__":
    raise SystemExit(main())
