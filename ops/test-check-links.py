#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test-check-links.py —— 护栏自检：确认文档规则真的会抓到反例。

对应 AGENTS.md「新行为配一条 check()」。故意在临时目录里制造四类违规，
验证 ops/check-links.py 不是永远绿；临时目录不属于任何 git 仓库，
顺带证明「扫描工作区、未 git add 的新文档也会被检查」。
"""
from __future__ import annotations

import importlib.util
import sys
import tempfile
from pathlib import Path

# 文件名带连字符，不能当模块名 import；按路径加载
_spec = importlib.util.spec_from_file_location(
    "check_links", Path(__file__).with_name("check-links.py"))
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
scan = _mod.scan

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("PASS " if ok else "FAIL ") + name + (f" | {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


def build(base: Path) -> Path:
    docs = base / "docs"
    docs.mkdir()
    (docs / "README.md").write_text(
        "# 文档地图\n\n| 层 | 文档 |\n|---|---|\n"
        "| L1 | [01-a](01-a.md) |\n| L2 | [02-b](02-b.md) |\n",
        encoding="utf-8")
    (docs / "01-a.md").write_text("# a\n", encoding="utf-8")
    (docs / "02-b.md").write_text("# b\n", encoding="utf-8")
    (base / "README.md").write_text("# root\n", encoding="utf-8")
    (base / "nonebot").mkdir()
    (base / "nonebot" / "mod.py").write_text("# 说明\n", encoding="utf-8")
    return docs


def has(errors: list[str], keyword: str) -> bool:
    return any(keyword in e for e in errors)


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        docs = build(base)

        errors, stats = scan(base)
        check("干净仓库全绿", not errors, str(errors)[:120])
        check("统计数量正确", stats["md"] == 4 and stats["registry"] == 2,
              str(stats))

        (base / "README.md").write_text(
            "# root\n见 [坏链接](docs/nope.md)\n", encoding="utf-8")
        errors, _ = scan(base)
        check("抓到失效 markdown 链接", has(errors, "失效链接"), str(errors)[:120])

        (base / "README.md").write_text("# root\n", encoding="utf-8")
        (base / "nonebot" / "mod.py").write_text(
            "# 见 docs/09（旧编号）\n", encoding="utf-8")
        errors, _ = scan(base)
        check("抓到注释里的裸文档编号", has(errors, "失效文档编号"), str(errors)[:120])

        (base / "nonebot" / "mod.py").write_text(
            "# 见 docs/99-已删除.md\n", encoding="utf-8")
        errors, _ = scan(base)
        check("抓到失效文档路径引用", has(errors, "失效文档引用"), str(errors)[:120])

        (base / "nonebot" / "mod.py").write_text("# 说明\n", encoding="utf-8")
        (docs / "03-c.md").write_text("# c\n", encoding="utf-8")
        errors, _ = scan(base)
        check("抓到未登记的新页（且不依赖 git add）",
              has(errors, "未登记"), str(errors)[:120])

        (docs / "03-c.md").unlink()
        (docs / "README.md").write_text(
            "# 文档地图\n\n| 层 | 文档 |\n|---|---|\n"
            "| L1 | [01-a](01-a.md) |\n| L2 | [02-b](02-b.md) |\n"
            "| L3 | [03-不存在](03-不存在.md) |\n", encoding="utf-8")
        errors, _ = scan(base)
        check("抓到登记了不存在的页", has(errors, "登记了不存在"), str(errors)[:120])

    print(f"\n=== {6 - len(FAILS)}/6 通过 ===" if not FAILS else
          f"\n=== FAIL: {', '.join(FAILS)} ===")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
