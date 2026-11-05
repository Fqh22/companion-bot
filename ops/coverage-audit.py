#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""coverage-audit.py —— 哪些 app_core 函数从头到尾没人提过。

**不是行覆盖率工具**，只回答一个问题：这个名字除了定义处，仓库里还有没有
第二个人提到它。没有 = 既没被测过、大概率也没被调用过（死代码或漏接线）。

启发式，别当铁律：回调注册、getattr 动态调用、测试里用字符串拼名字的，
都会被误判成「没人提」。所以输出分两档：
  DEAD       零引用（除定义处）
  ONLY-DEF   只在自己文件里被自己调用（跨层没接上）

用法：python ops/coverage-audit.py [-v]
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = Path(__file__).resolve().parent
CODE = HERE.parent / "nonebot"
if not (CODE / "app_core").is_dir():
    CODE = HERE.parent            # 部署后在 /opt/nonebot/ops 下
CORE = CODE / "app_core"
PLUGINS = CODE / "plugins"
TESTS = [HERE / n for n in ("test-nonebot-offline.py", "smoke-all.py",
                            "link-check.py", "smoke-plugins.py",
                            "lint-plugins.py", "replay-tone.py")]

# 这些名字是框架回调 / dunder / 明显被动态调用，别报
SKIP = {"__init__", "__enter__", "__exit__", "__aenter__", "__aexit__",
        "_conn", "handle", "rule", "permission"}


SKIP_DIRS = ("venv", "__pycache__", "site-packages", "node_modules", ".git")


def py_files(root: Path) -> list[Path]:
    """/opt/nonebot 下面有个 venv，里面有上万 .py。必须跳过，否则扫一次要几分钟。"""
    out: list[Path] = []
    for p in root.rglob("*.py"):
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        out.append(p)
    return out


def defs_of(path: Path) -> list[tuple[str, int]]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:  # noqa: BLE001
        return []
    out = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.append((node.name, node.lineno))
    return out


def main() -> int:
    verbose = "-v" in sys.argv
    files = sorted(CORE.glob("*.py"))
    corpus: dict[str, int] = {}
    sources: dict[Path, str] = {}

    def text_of(p: Path) -> str:
        if p not in sources:
            try:
                sources[p] = p.read_text(encoding="utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                sources[p] = ""
        return sources[p]

    # 统计每个名字出现次数：整个 nonebot + 脚本 + agentd
    scan_dirs = [CODE, HERE]
    for d in scan_dirs:
        for p in py_files(d):
            for name, _ in defs_of(p):
                pass
            t = text_of(p)
            for name in re.findall(r"\bdef\s+([A-Za-z_]\w*)\s*\(", t):
                corpus[name] = corpus.get(name, 0) + 0  # 占位，避免误算

    all_text = "\n".join(text_of(p) for d in scan_dirs for p in py_files(d))
    def_count: dict[str, int] = {}
    for f in files:
        for name, _ in defs_of(f):
            def_count[name] = def_count.get(name, 0) + 1

    dead, only_def, covered = [], [], 0
    for f in files:
        t_f = text_of(f)
        for name, line in defs_of(f):
            if name in SKIP or name.startswith("__"):
                continue
            # 出现次数 = 定义 + 调用。定义在上面的 def_count 里
            uses = len(re.findall(r"\b" + re.escape(name) + r"\b", all_text))
            used_elsewhere = uses - def_count.get(name, 1)
            if used_elsewhere <= 0:
                dead.append((f.name, name, line))
                continue
            # 是否有"自己文件之外"的引用
            in_other = any(
                re.search(r"\b" + re.escape(name) + r"\b", text_of(p))
                for d in scan_dirs for p in py_files(d)
                if p.name != f.name)
            if in_other:
                covered += 1
            else:
                only_def.append((f.name, name, line))

    total = covered + len(only_def) + len(dead)
    print("=== 函数覆盖审计（启发式）===")
    print("总计 %d 个函数：跨文件引用 %d，只在本文件 %d，零引用 %d"
          % (total, covered, len(only_def), len(dead)))
    if dead:
        print("\n--- 零引用（除定义外没人提）---")
        for fn, name, line in dead:
            print("  %-20s %-28s L%d" % (fn, name, line))
    if only_def:
        print("\n--- 只在本文件内被调用（跨层没接上）---")
        for fn, name, line in only_def:
            print("  %-20s %-28s L%d" % (fn, name, line))
    if verbose:
        print("\n（启发式：回调/动态调用会误报，看清单请人工判断）")
    return 0


if __name__ == "__main__":
    sys.exit(main())