#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""check-links.py —— 文档规则的唯一守门员（CI 与本地共用）。

检查三项：
  1. markdown 相对链接 / 图片目标存在（原能力）；
  2. 正文、注释、配置里出现的 `docs/NN...` 路径引用必须真实存在（含裸编号）；
  3. `docs/README.md` 登记表与 `docs/` 下实际编号文档一一对应。

为什么加 2/3：2026-11-16 把 19 篇合并为 5 篇后，AGENTS、代码注释里
仍有十几处旧编号（00 / 06 / 11 / 17）指向已删文件，
而原来只查 `[xxx](yyy)`，一个都抓不到。
范围改为扫描工作区而非 `git ls-files` —— 新建但还没 `git add` 的文档同样要检查。

排除：.git / .workbuddy / assets / CHANGELOG.md / 05-改动台账.md
      （后两者是历史流水，引用归档前路径是合理的）。
锚点仍只校验文件存在，不校验 #fragment（中文锚点跨渲染器规则不同）。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parent.parent
LINK = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
# 完整路径在 .md 处截断（避免把后面的中文说明一起吞进去）；裸编号单独一支
DOC_REF = re.compile(r"docs/\d{2}-[^\s`)\]\"'（）,，。；<>：]*?\.md|docs/\d{2}(?![\w-])")
REGISTRY_LINK = re.compile(r"\[[^\]]*\]\((\d{2}-[^\s)]+\.md)\)")
SKIP = ("http://", "https://", "mailto:", "#", "tel:")
EXCLUDE_DIRS = {".git", ".workbuddy", "assets", "__pycache__", "node_modules"}
# 护栏脚本本身含故意写坏的反例，不能被自己扫到
EXCLUDE_FILES = {"CHANGELOG.md", "05-改动台账.md",
                 "test-check-links.py", "test-guards.py"}
REF_EXTS = {".md", ".py", ".yaml", ".yml", ".toml", ".sh"}


def iter_files(root: Path, exts: set[str]) -> list[Path]:
    """扫描工作区（不依赖 git index），排除历史流水与生成目录。"""
    out = []
    for p in root.rglob("*"):
        if not p.is_file() or p.suffix not in exts:
            continue
        if any(x in p.parts for x in EXCLUDE_DIRS) or p.name in EXCLUDE_FILES:
            continue
        out.append(p)
    return sorted(out)


def rel(root: Path, p: Path) -> str:
    try:
        return str(p.relative_to(root))
    except ValueError:
        return str(p)


def target_path(root: Path, src: Path, raw: str) -> Path | None:
    t = raw.strip().split()[0] if raw.strip() else ""
    if not t or t.startswith(SKIP):
        return None
    t = unquote(t.split("#", 1)[0].split("?", 1)[0].strip())
    if not t or t.startswith("/"):
        return None
    p = (src.parent / t).resolve()
    try:
        p.relative_to(root.resolve())
    except ValueError:
        return None
    return p


def check_links(root: Path) -> tuple[list[str], int, int]:
    errors: list[str] = []
    n_links = 0
    mds = [p for p in iter_files(root, {".md"})]
    for src in mds:
        try:
            text = src.read_text(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            continue
        for m in LINK.finditer(text):
            n_links += 1
            p = target_path(root, src, m.group(1))
            if p is not None and not p.exists():
                errors.append(f"失效链接 {rel(root, src)} -> {m.group(1)}")
    return errors, n_links, len(mds)


def check_doc_refs(root: Path) -> tuple[list[str], int]:
    """路径引用：不仅查 markdown 链接，也查反引号/注释里的 docs/NN 字面量。"""
    errors: list[str] = []
    n_refs = 0
    docs = root / "docs"
    for src in iter_files(root, REF_EXTS):
        try:
            text = src.read_text(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            continue
        for m in DOC_REF.finditer(text):
            token = m.group(0)
            n_refs += 1
            if token.endswith(".md"):
                if not (root / token).is_file():
                    errors.append(f"失效文档引用 {rel(root, src)} -> {token}")
            else:
                # 裸编号（两位数字）：必须存在以该编号开头的现行文档
                prefix = token.rsplit("/", 1)[-1]
                if not list(docs.glob(prefix + "-*.md")):
                    errors.append(
                        f"失效文档编号 {rel(root, src)} -> {token}（请改成具体的新文档路径）")
    return errors, n_refs


def check_registry(root: Path) -> tuple[list[str], int]:
    """文档地图登记表 ↔ docs/ 实际编号文档，双向都不能有孤儿。"""
    errors: list[str] = []
    docs = root / "docs"
    reg = docs / "README.md"
    if not reg.is_file():
        return ["缺 docs/README.md（文档地图是唯一真相源）"], 0
    text = reg.read_text(encoding="utf-8", errors="replace")
    listed = set(REGISTRY_LINK.findall(text))
    actual = {p.name for p in docs.glob("[0-9][0-9]-*.md")}
    for name in sorted(actual - listed):
        errors.append(f"docs/{name} 存在但未登记到 docs/README.md")
    for name in sorted(listed - actual):
        errors.append(f"docs/README.md 登记了不存在的 {name}")
    return errors, len(listed)


def scan(root: Path) -> tuple[list[str], dict]:
    """核心入口，接受 root 便于 ops/test-check-links.py 在临时目录构造反例。"""
    e1, n_links, n_md = check_links(root)
    e2, n_refs = check_doc_refs(root)
    e3, n_reg = check_registry(root)
    return e1 + e2 + e3, {
        "md": n_md, "links": n_links, "refs": n_refs, "registry": n_reg,
    }


def main() -> int:
    errors, stats = scan(ROOT)
    print("检查了 %d 个 markdown 文件、%d 条链接、%d 处文档路径引用、%d 项登记："
          % (stats["md"], stats["links"], stats["refs"], stats["registry"]))
    if errors:
        print("结果：FAIL（%d 项）" % len(errors))
        for line in errors:
            print("  " + line)
        return 1
    print("结果：PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
