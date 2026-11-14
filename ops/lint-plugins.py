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
  红线 8      入口的记忆读写只走 MemoryPort（存量按 _MEM_ALLOWED 冻结，新增即 FAIL）
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
            bad.append(f"line {i}: app_core 不得 import nonebot（业务逻辑留 app_core，平台调用放 plugins/）")
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
    hidden = set(help_mod.hidden()) if hasattr(help_mod, "hidden") else set()
    registered = set(_scan_commands())
    fails: list[str] = []
    missing = sorted(registered - listed - hidden)
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
    return ["运行时路径写死了：" + "、".join(bad)
            + "（从 app_core/paths.py 的 DATA/QDATA 派生，别写字面量）"]


# 插件可以直接碰的 ChatCore 成员（接口线）。
# 新增一条要走这里登记——逼你先想清楚「这是有意暴露的接口，还是图方便」。
CORE_API = {
    # 回复主入口
    "reply", "reply_deadline", "last_prompts", "mood_event",
    # 记忆 / 人格 / 会话身份
    "memory", "persona", "persona_for", "reload_persona", "private_session",
    # 计数与状态
    "usage_stats", "config_issues", "config_status",
    "rate_per_minute", "cooldown", "enabled",
    # 子服务对象
    "scheduler", "pacing", "proactive", "files", "group", "chat",
    "whitelist", "admin_uid",
    # 图片与文件
    "caption_images",
    # 记忆门面（不许插件直接 core.memory.*）
    "memory_cleanup", "memory_superseded", "memory_forgotten",
    "memory_embed", "memory_restore",
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
                if name.startswith("_"):
                    fails.append(
                        f"{_rel(f)}:{i}: core.{name} 是私有成员，插件不得直接访问")
                    continue
                if name == "memory":
                    fails.append(
                        f"{_rel(f)}:{i}: 别直接调 core.memory.*，用 core.memory_* 门面"
                        f"（内部结构才能自由改）")
                    continue
                if name in CORE_API:
                    if name in CORE_API_DEPRECATED:
                        warns.append(f"{_rel(f)}:{i}: core.{name} 是遗留接口，建议改用公共方法")
                else:
                    fails.append(
                        f"{_rel(f)}:{i}: core.{name} 不在接口清单里"
                        f"（要暴露就先登记到 ops/lint-plugins.py 的 CORE_API）")
    return fails, sorted(set(warns))


# ---------------- 红线 8：记忆读写只走 MemoryPort ----------------
# AGENTS 红线 8 / docs/10：入口只认 recall / remember / forget / explain。
# 存量直连按「文件 -> 模块 -> 允许的符号」冻结：新增文件、新增模块、新增符号一律 FAIL；
# 迁掉一个符号就从这里删一个（白名单里没人用的条目会 WARN，提醒收紧）。
_MEM_MODULES = frozenset({"memory", "profile", "wiki", "kb", "diary", "learn"})

# 装配点与实现层豁免：chat 持有 Memory 实例，memory_port 就是适配器本身，
# memory/kb/wiki/learn/review/purge/web 是存储实现及它们的内部协作。
_MEM_EXEMPT = frozenset({
    "app_core/chat.py", "app_core/memory_port.py", "app_core/memory.py",
    "app_core/memory_store.py", "app_core/memory_index.py", "app_core/kb.py",
    "app_core/learn.py", "app_core/wiki.py", "app_core/review.py",
    "app_core/purge.py", "app_core/web.py", "app_core/meme.py",
})

_MEM_ALLOWED: dict[str, dict[str, set[str]]] = {
    "app_core/chat_turn.py": {
        "kb": {"recall"},                       # 索引没建好时的向量兜底
        "learn": {"profile_conf", "profile_fields", "profile_seen",
                  "recent_corrections", "skill_match"},   # 画像 / 技能 / 纠正清单
        "memory": {"embed", "recall"},          # 向量与召回（port 内部也走这里）
        "profile": {"compact"},                 # 画像压缩块
        "wiki": {"index_text"},                 # 知识索引
    },
    "app_core/tools.py": {
        "diary": {"list_days", "read"}, "kb": {"search_meta"},
        "learn": {"note_list", "note_write"}, "memory": {"read_full", "recall"},
        "wiki": {"read"},
    },
    "app_core/commands.py": {"diary": {"write_day"}, "learn": {"review", "weekly"}},
    "app_core/scheduling/scheduler.py": {
        "diary": {"write_day"}, "kb": {"index"},
        "learn": {"archive_raw", "review", "spaced_review", "weekly"},
        "wiki": {"archive_stale", "refresh", "weekly_text"},
    },
    "app_core/introspect.py": {"profile": {"MIN_CONF", "age_days", "effective_conf"}},
    "app_core/proactive_sources.py": {"learn": {"CURIOUS", "curious_ok", "read_list"}},
    "plugins/admin/__init__.py": {"learn": {"profile_fields", "profile_path"},
                                  "profile": {"clear", "forget"}},
    "plugins/commands/__init__.py": {
        "diary": {"list_days", "read", "write_day"}, "kb": {"index"},
        "learn": {"archive_raw", "note_list", "parse_ts", "review", "set_meta", "weekly"},
        "wiki": {"resolve", "set_verified"},
    },
    "plugins/schedule/__init__.py": {"kb": {"index"}, "wiki": {"refresh"}},
    # 纠正分诊：行为纠正写 corrections.md（这是它的目的地，不是记忆读穿）
    "app_core/correction.py": {"learn": {"CORRECTIONS", "merge_list"}},
}


def _mem_usage(p: Path):
    """返回 ([(行号, 模块, 符号)], {导入的模块})。

    只认 `from app_core import learn` 这类别名，以及 `self.memory.*` / `core.memory.*`。
    """
    try:
        tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"))
    except Exception:  # noqa: BLE001
        return [], set()
    alias = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.module == "app_core":
            for a in n.names:
                if a.name in _MEM_MODULES:
                    alias[a.asname or a.name] = a.name
        elif isinstance(n, ast.ImportFrom) and (n.module or "").startswith("app_core."):
            m = n.module.split(".")[1]
            if m in _MEM_MODULES:
                alias[n.module.split(".")[-1]] = m
    hits = []
    for n in ast.walk(tree):
        if not isinstance(n, ast.Attribute):
            continue
        base = n.value
        if isinstance(base, ast.Name) and base.id in alias:
            hits.append((n.lineno, alias[base.id], n.attr))
        elif (isinstance(base, ast.Attribute) and base.attr == "memory"
              and isinstance(base.value, ast.Name)
              and base.value.id in ("self", "core")):
            hits.append((n.lineno, "memory", n.attr))
    return hits, set(alias.values())


def check_memory_port():
    fails = []
    warns = []
    for p in sorted(list(CORE.rglob("*.py")) + list(PLUGINS.rglob("*.py"))):
        if "__pycache__" in p.parts:
            continue
        rel = p.relative_to(NB).as_posix()
        if rel in _MEM_EXEMPT:
            continue
        hits, imported = _mem_usage(p)
        if not hits and not imported:
            continue
        allowed = _MEM_ALLOWED.get(rel)
        if allowed is None:
            seen = sorted({f"{m}.{s}" for _, m, s in hits})
            fails.append(
                f"{rel}: 直接读写记忆（{', '.join(seen) or '仅 import'}）——红线 8："
                f"入口只走 MemoryPort；确有必要就登记到 ops/lint-plugins.py 的 _MEM_ALLOWED")
            continue
        for mod in sorted(imported - set(allowed)):
            fails.append(f"{rel}: import 了未登记的记忆模块 {mod}——红线 8 要显式登记")
        for ln, mod, sym in hits:
            if sym not in allowed.get(mod, set()):
                fails.append(
                    f"{rel}:{ln}: {mod}.{sym} 未登记——红线 8：新代码记忆读写走 "
                    f"MemoryPort（要保留存量先登记）")
        used = {(m, s) for _, m, s in hits}
        for mod, syms in allowed.items():
            for sym in sorted(syms):
                if (mod, sym) not in used and mod in imported:
                    warns.append(
                        f"{rel}: 白名单 {mod}.{sym} 已无人使用，可以从 _MEM_ALLOWED 删掉")
    return fails, sorted(set(warns))


def check_duplicate_commands() -> list[str]:
    """命令名不许重复：同名的两个 matcher，优先级高的会把另一个整个吞掉。

    为什么必须机器查：撞名**不报错**，NoneBot 只在启动日志里打一行
    `Duplicated prefix rule "/usage"`，沉在 journal 里没人看。实测 `/usage`
    就是这样被一个已移除的 agent 插件（priority=4 + block=True）吞掉的——commands 里
    那条「今日/近 7 天费用」从来没执行过，用户看到的永远是它的 token 数。
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


def check_slot_writes() -> list[str]:
    """R1：全库禁止对 extra_system 赋值/增删——槽位表是唯一入口。"""
    fails = []
    for f in sorted(list(CORE.rglob("*.py")) + list(PLUGINS.rglob("*.py"))):
        if "__pycache__" in f.parts:
            continue
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and node.func.attr in ("append", "extend", "insert", "pop", "remove"):
                v = node.func.value
                if isinstance(v, ast.Attribute) and v.attr == "extra_system":
                    fails.append(f"{_rel(f)}:{node.lineno}: 不许对 extra_system 调 .{node.func.attr}()"
                                 f"——注入块走 turn.set_slot()")
            if isinstance(node, (ast.Assign, ast.AugAssign)):
                tgts = node.targets if isinstance(node, ast.Assign) else [node.target]
                for t in tgts:
                    if isinstance(t, ast.Attribute) and t.attr == "extra_system":
                        fails.append(f"{_rel(f)}:{node.lineno}: 不许给 extra_system 赋值"
                                     f"——顺序由 chat_turn.SLOTS 决定")
    return fails


def check_slot_table() -> list[str]:
    """R2：SLOTS 表完整性——sid 唯一、key 唯一、字段是字面量。

    「唯一」而不是「书写递增」：渲染按 sorted(sid) 走，书写顺序不参与判断。
    """
    f = CORE / "chat_turn.py"
    try:
        tree = ast.parse(f.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        return [f"chat_turn.py 解析失败：{e}"]
    node = None
    for n in tree.body:
        if isinstance(n, ast.AnnAssign) and getattr(n.target, "id", "") == "SLOTS":
            node = n.value
    if node is None:
        return ["chat_turn.py: 找不到模块级 SLOTS 表（槽位顺序的唯一真源）"]
    fails = []
    sids, keys = [], []
    for elt in getattr(node, "elts", []):
        if not (isinstance(elt, ast.Call) and getattr(elt.func, "id", "") == "Slot"):
            fails.append("SLOTS 里出现非 Slot(...) 元素")
            continue
        if len(elt.args) < 3 or not all(isinstance(a, ast.Constant) for a in elt.args[:3]):
            fails.append("Slot 的 sid/key/nullable 必须是字面量")
            continue
        sids.append(elt.args[0].value)
        keys.append(elt.args[1].value)
        if not isinstance(elt.args[2].value, bool):
            fails.append(f"Slot({elt.args[1].value}) 的 nullable 必须是 bool")
    if len(sids) != len(set(sids)):
        fails.append(f"SLOTS 的 sid 有重复：{sids}")
    if len(keys) != len(set(keys)):
        fails.append(f"SLOTS 的 key 有重复：{keys}")
    return fails


def check_gather_local_sync() -> list[str]:
    """R3：asyncio.gather 里不许放同步阻塞取数（recall / knowledge / profile）。

    这些走 sqlite3 + numpy，gather 不会让它们并行，还会碰 check_same_thread。
    """
    banned = {"recall", "_prepare_recall", "_prepare_knowledge", "_prepare_profile"}
    fails = []
    for f in sorted(CORE.rglob("*.py")):
        if "__pycache__" in f.parts:
            continue
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "gather"):
                continue
            for a in node.args:
                name = None
                if isinstance(a, ast.Call):
                    name = getattr(a.func, "attr", None) or getattr(a.func, "id", None)
                if name in banned:
                    fails.append(f"{_rel(f)}:{node.lineno}: gather 里不许放 {name}"
                                 f"（LOCAL_SYNC，GIL + sqlite 线程亲和性，不会更快）")
    return fails


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
        all_fails += check_slot_writes()
        all_fails += check_slot_table()
        all_fails += check_gather_local_sync()
        mp_fails, mp_warns = check_memory_port()
        all_fails += mp_fails
        all_warns += mp_warns
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
