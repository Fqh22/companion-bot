#!/usr/bin/env python3
"""[infra] 分层依赖检查：层由每个模块 docstring 首行的 [domain] / [orchestration] / [infra] 标记声明。

规则（全部可机器验证，不靠自觉）：
- app_core 下每个 .py 必须有层标记，缺了直接 FAIL —— 新模块无法绕过分类。
- domain 只能依赖 domain / infra：不得 import 编排层，也不得直接 import IO 库。
- infra 不得 import 编排层（地基不能反过来依赖业务编排；依赖 domain 常量只 WARN）。
- kernel 不依赖业务包；adapters 不越过门面；plugins 的 import 收口在 B0b 单独开。
- 外包模块（web/asr/llm）只允许装配点与热路径 import，其余 WARN。

用法：
  python ops/lint-layers.py                 # 检查仓库里的 nonebot/app_core
  python ops/lint-layers.py --core DIR      # 检查指定目录（给反例自检用）
"""
from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
IO_MODULES = {"sqlite3", "aiosqlite", "httpx", "requests", "nonebot", "websockets"}
TAGS = {"[domain]": "domain", "[orchestration]": "orchestration", "[infra]": "infra"}
FAILS: list[str] = []
WARNS: list[str] = []


def _core_dir(arg: str | None) -> Path:
    if arg:
        return Path(arg).resolve()
    p = HERE.parent / "nonebot" / "app_core"
    if p.is_dir():
        return p
    return HERE.parent / "app_core"


# 模块级默认路径：ops/test-guards.py 会临时替换它来自检规则（命令行仍用 --core）。
CORE = _core_dir(None)


def _tag_of(path: Path) -> str | None:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:  # noqa: BLE001 - 语法错误由 lint-plugins 报，这里不重复
        return None
    doc = ast.get_docstring(tree) or ""
    first = doc.strip().splitlines()[0].strip() if doc.strip() else ""
    for tag, layer in TAGS.items():
        if first.startswith(tag):
            return layer
    return None


def _key_of(path: Path, core: Path) -> str:
    rel = path.relative_to(core).with_suffix("")
    return ".".join(rel.parts)


def _imports_of(path: Path) -> list[str]:
    """把 import 归一成 app_core.xxx 形式；相对导入按所在包解析。"""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:  # noqa: BLE001
        return []
    out: list[str] = []
    pkg = path.parent.name
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.extend(a.name for a in node.names if a.name.startswith("app_core"))
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = [pkg] if pkg != "app_core" else []
                for _ in range(node.level - 1):
                    base = base[:-1]
                mod = ".".join(base + ([node.module] if node.module else []))
                targets = [f"app_core.{mod}.{a.name}" for a in node.names] if mod else [
                    f"app_core.{a.name}" for a in node.names]
                out.extend(targets)
            elif node.module and node.module.startswith("app_core"):
                if node.module == "app_core":
                    out.extend(f"app_core.{a.name}" for a in node.names)
                else:
                    out.append(node.module)
    return out


def _io_imports(path: Path) -> list[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:  # noqa: BLE001
        return []
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module.split(".")[0])
    return sorted(found & IO_MODULES)


def scan(core: Path) -> dict[Path, str]:
    """返回 {文件: 层}，缺标记的直接记 FAIL。"""
    mods: dict[Path, str] = {}
    for p in sorted(core.rglob("*.py")):
        if "__pycache__" in p.parts:
            continue
        tag = _tag_of(p)
        if tag is None:
            FAILS.append(
                f"{p.relative_to(core.parent)}: 缺层标记 —— docstring 首行必须写 "
                f"[domain] / [orchestration] / [infra]（新模块不许绕过分类）")
            continue
        mods[p] = tag
    return mods


def _layer_of_target(target: str, layers: dict[str, str]) -> str | None:
    """app_core.foo.bar -> 找到最近的已声明模块。"""
    if not target.startswith("app_core"):
        return None
    rest = target[len("app_core"):].lstrip(".")
    parts = rest.split(".") if rest else []
    for i in range(len(parts), 0, -1):
        key = ".".join(parts[:i])
        if key in layers:
            return layers[key]
    return None


def check_direction(mods: dict[Path, str], core: Path) -> None:
    layers = {_key_of(p, core): t for p, t in mods.items()}
    for p, tag in mods.items():
        name = p.relative_to(core.parent)
        for target in _imports_of(p):
            other = _layer_of_target(target, layers)
            if other is None:
                continue
            if tag == "domain" and other == "orchestration":
                FAILS.append(
                    f"{name}: domain 不得 import 编排层：{target}"
                    "（纯规则留在 domain；编排放 orchestration，见 docs/02-系统与扩展.md）")
            if tag == "infra" and other == "orchestration":
                FAILS.append(
                    f"{name}: infra 不得 import 编排层：{target}（地基不能反向依赖业务）")
            if tag == "infra" and other == "domain":
                WARNS.append(f"{name}: infra 依赖 domain：{target}（常量可接受，逻辑请下沉）")


def check_domain_io(mods: dict[Path, str], core: Path) -> None:
    for p, tag in mods.items():
        if tag != "domain":
            continue
        bad = _io_imports(p)
        if bad:
            FAILS.append(
                f"{p.relative_to(core.parent)}: domain 里出现 IO import：{', '.join(bad)}"
                "（读写/网络放 orchestration 或 infra，domain 只做纯规则）")


def check_kernel(mods: dict[Path, str], core: Path) -> None:
    """kernel 只能依赖 infra / kernel；依赖 domain 或 orchestration 就是反向依赖。"""
    layers = {_key_of(p, core): t for p, t in mods.items()}
    for p in mods:
        if p.parent != core / "kernel":
            continue
        for t in _imports_of(p):
            other = _layer_of_target(t, layers)
            if other in ("domain", "orchestration"):
                FAILS.append(
                    f"{p.relative_to(core.parent)}: kernel 不得 import 业务层：{t}"
                    "（kernel 只放基础设施，依赖方向是业务 -> kernel）")


# 插件可以直接 import 的 app_core 模块：通道协议与基础设施，不是业务实现。
PLUGIN_ALLOWED = frozenset({
    "chat", "group",                      # 宿主插件自己实例化 ChatCore / GroupCore
    "config", "paths", "pacing", "delivery", "message",
    "engine", "runner", "turn", "files", "asr", "tts", "image_provider",
    "kernel", "adapters",
})

# 冻结存量：插件 -> 允许继续直接 import 的业务模块。
# 这是迁移脚手架，不是永久门禁：到期日之后一律 FAIL；迁掉一个删一个。
PLUGIN_LEGACY = {
    "admin/__init__.py": {"help", "introspect", "learn", "audit", "tools", "profile"},
    "commands/__init__.py": {"diary", "introspect", "kb", "learn", "meme", "mood", "remind", "wiki"},
    "schedule/__init__.py": {"kb", "meme", "wiki", "scheduler"},
    "group/__init__.py": {"group"},
    "proactive/__init__.py": {"proactive_runtime", "proactive_channels"},
    "chat/__init__.py": {"meme", "mood", "relational"},
    "feishu/__init__.py": {"mood", "proactive_channels", "config"},
}
PLUGIN_LEGACY_DEADLINE = "2026-12-31"


def check_plugins(core: Path) -> None:
    """插件不得直接 import 业务实现；只能走 core 门面或通道协议模块。

    为什么需要：插件是平台胶水，业务内部结构一旦被插件 import 焊死，
    改内部就要同时改 7 个插件（2026-11-11 起 AGENTS 红线 3 的原意）。
    存量按「文件 -> 模块」冻结，带到期日；新文件/新模块直接 FAIL。
    """
    from datetime import date

    plugins = core.parent / "plugins"
    if not plugins.is_dir():
        return
    expired = date.today().isoformat() > PLUGIN_LEGACY_DEADLINE
    for p in sorted(plugins.rglob("*.py")):
        rel = p.relative_to(plugins).as_posix()
        legacy = PLUGIN_LEGACY.get(rel, set())
        for t in _imports_of(p):
            parts = t.split(".")
            mod = parts[1] if len(parts) > 1 else ""
            if not mod or mod in PLUGIN_ALLOWED:
                continue
            if mod in legacy and not expired:
                WARNS.append(
                    f"{p.relative_to(core.parent)}: 仍直接 import app_core.{mod}"
                    f"（迁移白名单，{PLUGIN_LEGACY_DEADLINE} 到期；改走 core 门面）")
            elif mod in legacy and expired:
                FAILS.append(
                    f"{p.relative_to(core.parent)}: 迁移白名单已到期（{PLUGIN_LEGACY_DEADLINE}），"
                    f"仍直接 import app_core.{mod}")
            else:
                FAILS.append(
                    f"{p.relative_to(core.parent)}: 插件不得直接 import 业务实现：app_core.{mod}"
                    "（走 plugins.chat 的 core 门面；通道协议模块见 PLUGIN_ALLOWED）")


def check_adapters(core: Path) -> None:
    adapters = core.parent / "adapters"
    if not adapters.is_dir():
        return
    banned = {"app_core.chat", "app_core.chat_turn", "app_core.tool_loop"}
    for p in adapters.rglob("*.py"):
        for t in _imports_of(p):
            if t in banned:
                FAILS.append(
                    f"{p.relative_to(core.parent)}: 适配层不得 import 内部实现：{t}"
                    "（只走 engine / runner / commands / delivery 门面）")


# 已知循环依赖基线：只许减少，不许新增。拆掉一组就删一条（会 WARN 提醒）。
CYCLE_BASELINE = {
    ("learn.py", "review.py"),
    ("learn.py", "wiki.py"),
}


def check_cycles(mods: dict[Path, str], core: Path) -> None:
    """模块间不允许新增循环依赖；存量基线里的可以继续存在，但删掉要同步收线。"""
    graph: dict[str, set[str]] = {}
    for p in mods:
        rel = p.relative_to(core).as_posix()
        deps: set[str] = set()
        for t in _imports_of(p):
            key = t[len("app_core."):] if t.startswith("app_core.") else t
            q = core / (key.replace(".", "/") + ".py")
            if q in mods:
                deps.add(q.relative_to(core).as_posix())
        graph[rel] = deps
    found: set[tuple[str, ...]] = set()
    def dfs(start: str, node: str, path: list[str], seen: set[str]) -> None:
        for nxt in graph.get(node, ()):
            if nxt == start and len(path) > 1:
                found.add(tuple(sorted(set(path))))
            elif nxt not in seen and len(path) <= 4:
                dfs(start, nxt, path + [nxt], seen | {nxt})
    for start in graph:
        dfs(start, start, [start], {start})
    for group in sorted(found):
        if group not in CYCLE_BASELINE:
            FAILS.append(
                f"新增循环依赖：{' <-> '.join(group)}"
                "（拆成单向；基线见 lint-layers.py 的 CYCLE_BASELINE）")
    for group in sorted(CYCLE_BASELINE - found):
        WARNS.append(
            f"循环基线里这组已经不存在：{' <-> '.join(group)}（删掉 CYCLE_BASELINE 里这行，收紧）")


OUTSOURCED = frozenset({"web", "asr", "llm"})
OUTSOURCED_ALLOWED = frozenset({
    "chat", "chat_turn", "memory", "tools", "engine", "group", "__init__", "perception",
})


def check_outsourced(core: Path) -> None:
    for p in sorted(core.glob("*.py")):
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
                    WARNS.append(f"{p.name}:{i}: import 外包模块 {mod}（见 docs/01-定义与决策.md）")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--core", default=None, help="app_core 目录（默认仓库内路径）")
    args = ap.parse_args()
    core = _core_dir(args.core)
    if not core.is_dir():
        print(f"FATAL 找不到 app_core：{core}")
        return 2
    mods = scan(core)
    check_direction(mods, core)
    check_domain_io(mods, core)
    check_kernel(mods, core)
    check_plugins(core)
    check_adapters(core)
    check_cycles(mods, core)
    check_outsourced(core)
    for x in WARNS:
        print("WARN", x)
    if FAILS:
        print("=== 分层失败 ===")
        for x in FAILS:
            print("FAIL", x)
        return 1
    print(f"分层依赖：PASS（{len(mods)} 个模块都有层标记，没有反向依赖）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
