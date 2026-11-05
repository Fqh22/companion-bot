#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""new-plugin.py —— 按《插件规范 v2》生成 NoneBot2 插件骨架。

用法：
    python new-plugin.py --name my_plugin --short "一句话说明" --desc "两三句说明"

生成：
    nonebot/app_core/<name>.py           纯逻辑（可离线单测）
    nonebot/plugins/<name>/__init__.py   适配层
    nonebot/plugins/<name>/README.md
    （config.yaml 片段只打印，不自动改，避免误改线上配置）
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent                       # 仓库根目录/
TEMPLATE = PROJECT / "templates/plugin"
CORE_DIR = PROJECT / "nonebot" / "app_core"
PLUGIN_DIR = PROJECT / "nonebot" / "plugins"

# (模板文件, 生成路径模板)
FILES = [
    ("app_core_module.py.tmpl", CORE_DIR / "{name}.py"),
    ("plugin_init.py.tmpl", PLUGIN_DIR / "{name}" / "__init__.py"),
    ("README.md", PLUGIN_DIR / "{name}" / "README.md"),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True, help="snake_case 插件名")
    ap.add_argument("--short", default="", help="一句话说明")
    ap.add_argument("--desc", default="", help="两三句说明")
    ap.add_argument("--force", action="store_true", help="覆盖已存在的文件")
    a = ap.parse_args()

    name = a.name.strip()
    if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
        print("插件名必须是 snake_case（小写字母/数字/下划线，字母开头）")
        return 2
    if not TEMPLATE.is_dir():
        print(f"找不到模板目录：{TEMPLATE}")
        return 2

    short = a.short or "TODO：一句话说明"
    desc = a.desc or short
    repl = {
        "{{name}}": name,
        "{{short}}": short,
        "{{desc}}": desc,
    }

    targets = []
    for tpl_name, out_pattern in FILES:
        src = TEMPLATE / tpl_name
        if not src.exists():
            print(f"模板缺文件：{tpl_name}")
            return 2
        out = Path(str(out_pattern).replace("{name}", name))
        if out.exists() and not a.force:
            print(f"文件已存在：{out}（要覆盖加 --force）")
            return 2
        targets.append((src, out))

    for src, out in targets:
        out.parent.mkdir(parents=True, exist_ok=True)
        text = src.read_text(encoding="utf-8")
        for k, v in repl.items():
            text = text.replace(k, v)
        out.write_text(text, encoding="utf-8", newline="\n")
        print(f"  生成 {out.relative_to(PROJECT)}")

    snippet = (TEMPLATE / "config_snippet.yaml").read_text(encoding="utf-8")
    for k, v in repl.items():
        snippet = snippet.replace(k, v)

    print("\n下一步：")
    print("  1. 把下面这段加到 /opt/nonebot/config.yaml 末尾：\n")
    for line in snippet.splitlines():
        if line.strip() and not line.startswith("#"):
            print("     " + line)
    print("\n  2. 补 app_core 里的业务逻辑，再跑：python ops/lint-plugins.py")
    print("  3. 部署后跑离线回归：/opt/nonebot/ops/offline-test.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
