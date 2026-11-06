#!/usr/bin/env python3
"""kernel 层测试：Clock / Settings / DB 事务。"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = Path(__file__).resolve().parent
CODE = HERE.parent / "nonebot"
if not (CODE / "app_core").is_dir():
    CODE = HERE.parent
sys.path.insert(0, str(CODE))

from app_core.kernel import FakeClock, Settings  # noqa: E402
from app_core.kernel import db as kdb  # noqa: E402

PASS = FAIL = 0


def check(name, ok, extra=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"PASS {name}" + (f" | {extra}" if extra else ""))
    else:
        FAIL += 1
        print(f"FAIL {name}" + (f" | {extra}" if extra else ""))


def main() -> int:
    clock = FakeClock(1_700_000_000)
    check("FakeClock 可读", clock.now() == 1_700_000_000)
    clock.advance(60)
    check("FakeClock 可推进", clock.now() == 1_700_000_060)

    s = Settings.from_mapping({"llm": {"temperature": 1.2}, "admin": {"user_id": "1"}})
    check("Settings 取嵌套值", s.get("llm.temperature") == 1.2)
    check("Settings 缺省值", s.get("llm.nope", 42) == 42)
    check("Settings 取段", s.section("admin").get("user_id") == "1")

    tmp = Path(tempfile.mkdtemp(prefix="kernel-test-"))
    p = tmp / "t.db"
    with kdb.transaction(p) as c:
        c.execute("CREATE TABLE x (v INTEGER)")
        c.execute("INSERT INTO x VALUES (1)")
    with kdb.transaction(p) as c:
        check("事务提交可见", c.execute("SELECT COUNT(*) FROM x").fetchone()[0] == 1)
    check("WAL 已开启", sqlite3.connect(p).execute("PRAGMA journal_mode").fetchone()[0] == "wal")

    print(f"\n=== {PASS}/{PASS + FAIL} 通过 ===")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
