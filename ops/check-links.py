#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""check-links.py —— 检查 Markdown 里的相对链接/图片是否真的存在。

为什么：文档重构（README、docs/README.md、CONTRIBUTING）之后，链接腐烂是常态；
CI 里跑一遍，改名/移动文件时会被拦住，而不是等读者点开 404。

范围：git 里所有 *.md；跳过 http(s)/mailto/anchor-only/绝对路径。
锚点只校验文件存在，不校验 #fragment（中文锚点跨渲染器规则不同，容易误报）。
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parent.parent
LINK = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
SKIP = ("http://", "https://", "mailto:", "#", "tel:")


def md_files() -> list[Path]:
    try:
        out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "*.md"],
                             capture_output=True, text=True, encoding="utf-8",
                             errors="replace", check=True).stdout
        return [ROOT / line for line in out.splitlines() if line.strip()]
    except Exception:  # noqa: BLE001
        return [p for p in ROOT.rglob("*.md")
                if ".git" not in p.parts and "__pycache__" not in p.parts]


def target_path(src: Path, raw: str) -> Path | None:
    t = raw.strip().split()[0] if raw.strip() else ""
    if not t or t.startswith(SKIP):
        return None
    t = unquote(t.split("#", 1)[0].split("?", 1)[0].strip())
    if not t or t.startswith("/"):
        return None
    p = (src.parent / t).resolve()
    try:
        p.relative_to(ROOT)
    except ValueError:
        return None
    return p


def main() -> int:
    missing = []
    n_links = 0
    for src in md_files():
        try:
            text = src.read_text(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            continue
        for m in LINK.finditer(text):
            n_links += 1
            p = target_path(src, m.group(1))
            if p is None:
                continue
            if not p.exists():
                missing.append("%s -> %s" % (src.relative_to(ROOT), m.group(1)))
    if missing:
        print("发现失效链接：")
        for line in missing:
            print("  " + line)
        print("\n结果：FAIL（%d 条失效）" % len(missing))
        return 1
    print("检查了 %d 个 markdown 文件、%d 条链接：PASS" % (len(md_files()), n_links))
    return 0


if __name__ == "__main__":
    sys.exit(main())
