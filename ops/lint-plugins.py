#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""lint-plugins.py —— 按《插件规范 v2》检查 NoneBot2 的分层与约定。

用法：
    python lint-plugins.py            # 检查全部
    python lint-plugins.py chat       # 只查一个插件
退出码：有 FAIL 时返回 1（可接进部署脚本）。

检查项：
  app_core/   语法、不得 import nonebot（分层红线）、不得裸 print
  plugins/    每个包有 __init__.py、语法、不得裸 print、README 缺失记 WARN
  跨插件      命令名不得重复（重名时高优先级的 matcher 会把另一个整个吞掉）
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
# 两种布局都要支持：
#   仓库里   ops/lint-plugins.py        → 代码在 ../nonebot
#   部署后   /opt/nonebot/ops/lint-…py   → 代码就在上一层
NB = HERE.parent / "nonebot"
if not (NB / "app_core").is_dir():
    NB = HERE.parent
CORE = NB / "app_core"
PLUGINS = NB / "plugins"


def _rel(p: Path) -> str:
    try:
        return str(p.relative_to(HERE.parent))
    except ValueError:
        return str(p)


def check_syntax(p: Path) -> list[str]:
    try:
        ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
    except SyntaxError as e:
        return [f"语法错误 line {e.lineno}: {e.msg}"]
    except Exception as e:  # noqa: BLE001
        return [f"读取失败: {type(e).__name__}"]
    return []


def check_no_nonebot(p: Path) -> list[str]:
    """app_core 必须能脱离框架离线跑。"""
    bad = []
    for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
        s = line.strip()
        if s.startswith(("import nonebot", "from nonebot")):
            bad.append(f"line {i}: app_core 不得 import nonebot")
    return bad


def check_no_print(p: Path) -> list[str]:
    """统一走 logger，禁止裸 print。"""
    bad = []
    for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
        s = line.strip()
        if s.startswith("print(") and not s.startswith("#"):
            bad.append(f"line {i}: 用 logger，别用 print")
    return bad


def lint_core(name: str | None) -> tuple[list[str], list[str]]:
    fails: list[str] = []
    warns: list[str] = []
    files = sorted(CORE.glob("*.py"))
    if name:
        files = [f for f in files if f.stem == name]
    for f in files:
        # 不能跳过 __init__.py：`import nonebot` 写在这里同样是分层红线违规，
        # 之前整包跳过，等于给了一个后门（实测确实漏掉了）。
        problems = check_syntax(f) + check_no_nonebot(f) + check_no_print(f)
        for msg in problems:
            fails.append(f"{_rel(f)}: {msg}")
    if not files:
        warns.append("app_core 没有匹配的模块")
    return fails, warns


def lint_plugins(name: str | None) -> tuple[list[str], list[str]]:
    fails: list[str] = []
    warns: list[str] = []
    dirs = sorted(d for d in PLUGINS.iterdir()
                  if d.is_dir() and not d.name.startswith(("_", "."))) if PLUGINS.is_dir() else []
    if name:
        dirs = [d for d in dirs if d.name == name]
    for d in dirs:
        pkg = d / "__init__.py"
        if not pkg.is_file():
            fails.append(f"{_rel(d)}: 缺 __init__.py（NoneBot 不会加载）")
            continue
        for msg in check_syntax(pkg) + check_no_print(pkg):
            fails.append(f"{_rel(pkg)}: {msg}")
        if not (d / "README.md").is_file():
            warns.append(f"{_rel(d)}: 缺 README.md（规范要求必填）")
    if not dirs:
        warns.append("plugins 下没有匹配的插件")
    return fails, warns


def _scan_commands() -> dict[str, list[str]]:
    """扫出所有 on_command 注册的命令名 → ["文件:行号"]。两处检查共用。"""
    seen: dict[str, list[str]] = {}
    for f in sorted(PLUGINS.rglob("*.py")):
        try:
            tree = ast.parse(f.read_text(encoding="utf-8", errors="replace"))
        except Exception:  # noqa: BLE001
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            fn = node.func
            fname = getattr(fn, "id", None) or getattr(fn, "attr", None)
            if fname not in ("on_command", "on_shell_command"):
                continue
            a = node.args[0]
            if isinstance(a, ast.Constant) and isinstance(a.value, str):
                seen.setdefault(a.value, []).append(f"{_rel(f)}:{node.lineno}")
    return seen


def check_help_coverage() -> tuple[list[str], list[str]]:
    """注册表 ↔ /help 清单必须一致。

    /help 以前在 admin 里手写，注册 36 条只列了 6 条——能力做了但没入口。
    现在清单是 app_core/help.py，这条检查保证它不会再次漂走。
    """
    try:
        if str(NB) not in sys.path:
            sys.path.insert(0, str(NB))
        from app_core import help as help_mod
    except Exception as e:  # noqa: BLE001
        return [], [f"读不到 app_core/help.py：{type(e).__name__}: {e}"]
    listed = set(help_mod.names())
    registered = set(_scan_commands())
    fails: list[str] = []
    missing = sorted(registered - listed)
    if missing:
        fails.append("这些命令注册了但没写进 /help：/" + "、/".join(missing))
    extra = sorted(listed - registered)
    if extra:
        fails.append("这些命令写进了 /help 但没注册：/" + "、/".join(extra))
    return fails, []


def check_no_hardcoded_paths() -> list[str]:
    """运行时路径不许写死 /opt：必须从 app_core/paths.py 取。

    2026-11-05 的漏网：改成 QQBOT_ROOT 时只替换了 Path("/opt/...")，
    字符串形态的 "/opt/nonebot/data/chat.db" 留在 scheduler / agent / proactive 里，
    结果设了根还是会写回 /opt。人眼靠不住，交给机器查。
    """
    bad: list[str] = []
    for d in (CORE, PLUGINS):
        for p in sorted(d.rglob("*.py")):
            if p.name == "paths.py":
                continue
            for i, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                t = line.strip()
                if t.startswith("#"):
                    continue
                if '"/opt/' in t or "'/opt/" in t:
                    bad.append(f"{_rel(p)}:{i}")
    if not bad:
        return []
    return ["运行时路径写死了（改用 app_core/paths.py）：" + "、".join(bad)]


# 插件可以直接碰的 ChatCore 成员（接口线）。
# 新增一条要走这里登记——逼你先想清楚「这是有意暴露的接口，还是图方便」。
CORE_API = {
    # 回复主入口
    "reply", "reply_deadline", "last_prompts", "mood_event",
    # 记忆 / 人格
    "memory", "persona", "persona_for", "reload_persona",
    # 计数与状态
    "usage_stats", "config_issues", "config_status",
    "rate_per_minute", "cooldown", "enabled",
    # 子服务对象
    "scheduler", "pacing", "proactive", "files", "group", "chat",
    "whitelist", "admin_uid",
    # 图片与文件
    "caption_images",
    # 写历史（插件发消息后必须回写，否则机器人不记得自己说过什么）
    "record_assistant", "append_to_message",
    # 兼容入口
    "remember_safe", "catch_up_summary", "complete_raw", "init_db",
}

# 没有遗留项了。以后要收内部接口，先在这里登记，再逐个迁移调用点。
CORE_API_DEPRECATED: set[str] = set()


def check_core_api() -> tuple[list[str], list[str]]:
    """插件只许调登记过的 core 成员。

    为什么需要：ChatCore 有 40 多个方法/属性，其中一半是内部实现。
    没有边界时，插件随手 core.append_to_message(...) 就把内部结构焊死了；
    以后想改内部，得同时改 7 个插件。这条检查把「公共接口」变成可执行的约定。
    """
    import re

    pat = re.compile(r"\bcore\.([A-Za-z_]\w*)")
    fails: list[str] = []
    warns: list[str] = []
    for f in sorted(PLUGINS.rglob("*.py")):
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            continue
        # 只查持有 ChatCore 的插件。group 里的 core 是 GroupCore（另一个类），
        # 它自己的 budget_left 是合法接口，不属于这条检查。
        if "from plugins.chat import core" not in text and f.name != "__init__.py":
            continue
        if f.parent.name == "group":
            continue
        lines = text.splitlines()
        for i, line in enumerate(lines, 1):
            if line.strip().startswith("#"):
                continue
            for m in pat.finditer(line):
                name = m.group(1)
                if name in CORE_API:
                    if name in CORE_API_DEPRECATED:
                        warns.append(f"{_rel(f)}:{i}: core.{name} 是遗留接口，建议改用公共方法")
                else:
                    fails.append(
                        f"{_rel(f)}:{i}: core.{name} 不在接口清单里"
                        f"（要暴露就先登记到 ops/lint-plugins.py 的 CORE_API）")
    return fails, sorted(set(warns))


def check_duplicate_commands() -> list[str]:
    """命令名不许重复：同名的两个 matcher，优先级高的会把另一个整个吞掉。

    为什么必须机器查：撞名**不报错**，NoneBot 只在启动日志里打一行
    `Duplicated prefix rule "/usage"`，沉在 journal 里没人看。实测 `/usage`
    就是这样被 agent 插件（priority=4 + block=True）吞掉的——commands 里那条
    「今日/近 7 天费用」从来没执行过，用户看到的永远是 agentd 的 token 数。
    """
    seen: dict[str, list[str]] = {}
    for f in sorted(PLUGINS.rglob("*.py")):
        try:
            tree = ast.parse(f.read_text(encoding="utf-8", errors="replace"))
        except Exception:  # noqa: BLE001
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            fn = node.func
            fname = getattr(fn, "id", None) or getattr(fn, "attr", None)
            if fname not in ("on_command", "on_shell_command"):
                continue
            a = node.args[0]
            if isinstance(a, ast.Constant) and isinstance(a.value, str):
                seen.setdefault(a.value, []).append(f"{_rel(f)}:{node.lineno}")
    return [f"命令重名：/{k} 注册了 {len(v)} 次（{', '.join(v)}）——"
            f"优先级高的会吞掉低的，用户只会看到其中一个"
            for k, v in sorted(seen.items()) if len(v) > 1]


def main() -> int:
    name = sys.argv[1].strip() if len(sys.argv) > 1 else None
    if not NB.is_dir():
        print(f"找不到目录：{NB}")
        return 2

    all_fails: list[str] = []
    all_warns: list[str] = []

    core_fails, core_warns = lint_core(name)
    plug_fails, plug_warns = lint_plugins(name)
    all_fails += core_fails + plug_fails
    if not name:                      # 重名/覆盖率都是跨插件的，只在全量检查时看
        all_fails += check_duplicate_commands()
        all_fails += check_no_hardcoded_paths()
        api_fails, api_warns = check_core_api()
        all_fails += api_fails
        all_warns += api_warns
        help_fails, help_warns = check_help_coverage()
        all_fails += help_fails
        all_warns += help_warns
    all_warns += core_warns + plug_warns

    print("=== FAIL ===")
    print("\n".join(all_fails) if all_fails else "（无）")
    print("\n=== WARN ===")
    print("\n".join(all_warns) if all_warns else "（无）")

    status = "FAIL" if all_fails else ("WARN" if all_warns else "PASS")
    print(f"\n结果：{status}（FAIL {len(all_fails)} / WARN {len(all_warns)}）")
    return 1 if all_fails else 0


if __name__ == "__main__":
    sys.exit(main())
