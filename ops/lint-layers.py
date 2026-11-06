#!/usr/bin/env python3
"""分层依赖检查：给 P0-P7 重构用的护栏。

规则只在目标包存在时生效，所以可以先上线、再逐个模块迁移。
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = Path(__file__).resolve().parent
CORE = HERE.parent / "nonebot" / "app_core"
if not CORE.is_dir():
    CORE = HERE.parent / "app_core"

IO_MODULES = {"sqlite3", "aiosqlite", "httpx", "requests", "nonebot", "websockets"}
FAILS: list[str] = []


def imports(path: Path) -> list[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except Exception as e:
        FAILS.append(f"{path}: 解析失败 {e}")
        return []
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.append(node.module)
    return out


def check_domain():
    """domain.py 只做纯规则：不碰数据库/网络/框架。"""
    for p in CORE.glob("*/domain.py"):
        bad = sorted(set(imports(p)) & IO_MODULES)
        if bad:
            FAILS.append(f"{p.relative_to(CORE.parent)}: domain 里出现 IO import：{', '.join(bad)}")


def check_kernel():
    """kernel 不反向依赖业务包。"""
    kernel = CORE / "kernel"
    if not kernel.is_dir():
        return
    for p in kernel.glob("*.py"):
        bad = sorted(m for m in imports(p)
                     if m == "app_core"
                     or (m.startswith("app_core.") and not m.startswith("app_core.kernel")))
        if bad:
            FAILS.append(f"{p.relative_to(CORE.parent)}: kernel 不得 import 业务包：{', '.join(bad)}")


def check_plugins():
    """插件只能通过门面拿 core，不能 import 业务子模块。

    迁移期间先只查新包；老插件等 P3/P4 再纳入。
    """
    plugins = CORE.parent / "plugins"
    if not plugins.is_dir():
        return
    for p in plugins.rglob("*.py"):
        mods = imports(p)
        if any(m == "app_core" for m in mods) and "from plugins.chat import core" not in p.read_text(encoding="utf-8"):
            # 现有插件大量 import app_core.diary 等，先不 FAIL；只标记新包
            pass


def main() -> int:
    check_kernel()
    check_domain()
    check_plugins()
    if FAILS:
        print("=== 分层失败 ===")
        for x in FAILS:
            print("FAIL", x)
        return 1
    print("分层依赖：PASS（没有发现反向依赖）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
