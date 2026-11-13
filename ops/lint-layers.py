#!/usr/bin/env python3
"""分层依赖检查：给 P0-P7 重构用的护栏。

规则只在目标包存在时生效，所以可以先上线、再逐个模块迁移。
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
CORE = HERE.parent / "nonebot" / "app_core"
if not CORE.is_dir():
    CORE = HERE.parent / "app_core"

IO_MODULES = {"sqlite3", "aiosqlite", "httpx", "requests", "nonebot", "websockets"}
FAILS: list[str] = []
WARNS: list[str] = []

# 三层（见 docs/06-扩展与移植.md）：
#   domain        纯规则：可以被任意层依赖，自己只能依赖 domain / infra
#   orchestration 编排：调多个领域模块、碰 IO，可以被 adapters/plugins 依赖
#   infra         基础设施：paths/config/files/kernel，谁都能用
DOMAIN = frozenset({
    "delivery", "pacing", "prompt", "intent", "textsim", "vec",
    "memory_score", "mood_domain", "followup_domain", "memory_common",
})
ORCH = frozenset({
    "engine", "runner", "commands", "runtime", "introspect", "proactive_runtime",
    "chat", "chat_turn", "tool_loop", "scheduler", "proactive",
    "learn", "review", "memory", "memory_store", "memory_index",
    "wiki", "kb", "diary", "followup", "followup_store",
    "mood", "mood_store", "profile", "contradict", "summarize",
    "status", "watch", "meme", "web", "purge", "maintenance",
    "relational", "attention", "retrieval", "memgraph", "vocab",
    "identity", "approval",
})


def _targets(path: Path) -> list[str]:
    """把 import 归一成 `app_core.xxx` 形式，含 from app_core import xxx。"""
    out: list[str] = []
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except Exception:
        return out
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.extend(a.name for a in node.names if a.name.startswith("app_core"))
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module == "app_core":
                out.extend(f"app_core.{a.name}" for a in node.names)
            elif node.module.startswith("app_core."):
                out.append(node.module)
    return out


def check_domain_layers():
    """domain 不得反向依赖编排层：否则纯规则就绑上了 IO/会话状态。"""
    for name in sorted(DOMAIN):
        p = CORE / f"{name}.py"
        if not p.is_file():
            continue
        for t in _targets(p):
            mod = t.split(".")[1] if t.count(".") >= 1 else ""
            if mod in ORCH:
                FAILS.append(f"app_core/{name}.py: domain 不得 import 编排层：{t}"
                             "（编排逻辑留在 orchestration；domain 只做纯规则，见 docs/06）")


def check_adapters():
    """适配层只走门面：engine / runner / commands / delivery，不碰内部实现。"""
    adapters = CORE.parent / "adapters"
    if not adapters.is_dir():
        return
    banned = {"app_core.chat", "app_core.chat_turn", "app_core.tool_loop"}
    for p in adapters.rglob("*.py"):
        for t in _targets(p):
            if t in banned:
                FAILS.append(f"{p.relative_to(CORE.parent)}: 适配层不得 import 内部实现：{t}"
                             "（只走 engine / runner / commands / delivery 门面）")


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
            FAILS.append(f"{p.relative_to(CORE.parent)}: domain 里出现 IO import：{', '.join(bad)}"
                         "（读写/网络放 orchestration，domain 只做纯规则）")


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
            FAILS.append(f"{p.relative_to(CORE.parent)}: kernel 不得 import 业务包：{', '.join(bad)}"
                         "（kernel 只放基础设施，业务依赖反过来）")


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


# 外包模块（docs/10）：只做「一次调用 → 一个结果」，不持有跨轮状态。
# 只有装配点和热路径可以 import；其它模块要外部能力，走这些模块的接口。
OUTSOURCED = frozenset({"web", "asr", "llm"})
OUTSOURCED_ALLOWED = frozenset({
    "chat", "chat_turn", "memory", "tools", "engine", "group", "__init__",
    # 感知层本身就是外包入口：天气源失败时退到 web.search，属于装配逻辑
    "perception",
})


def check_outsourced() -> None:
    """非装配点/热路径 import 外包模块 → WARN（先观察，不阻断）。"""
    for p in sorted(CORE.glob("*.py")):
        name = p.stem
        if name in OUTSOURCED or name in OUTSOURCED_ALLOWED:
            continue
        for i, line in enumerate(
                p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            t = line.strip()
            if t.startswith("#"):
                continue
            for mod in OUTSOURCED:
                hit = (f"app_core.{mod}" in t) or (
                    t.startswith("from app_core import") and re.search(rf"\b{mod}\b", t))
                if hit:
                    WARNS.append(f"{p.name}:{i}: import 外包模块 {mod}（见 docs/10）")


def main() -> int:
    check_kernel()
    check_domain()
    check_domain_layers()
    check_adapters()
    check_plugins()
    check_outsourced()
    if WARNS:
        for x in WARNS:
            print("WARN", x)
    if FAILS:
        print("=== 分层失败 ===")
        for x in FAILS:
            print("FAIL", x)
        return 1
    print("分层依赖：PASS（没有发现反向依赖）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
