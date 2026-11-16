#!/usr/bin/env python3
"""[infra] 配置三处同步：DEFAULTS / example.yaml / 面板键表。

为什么需要：2026-11-16 核对发现 `weather` 段只在 yaml 里有、DEFAULTS 没有，
代码靠 `or {}` 兜底；`llm.prepare_parallel` 写在 yaml、代码读 `chat.prepare_parallel`，
开关从来没生效过（默认恰好是 false，所以没人发现）。同一个事实四处手抄，
迟早会漂移——这条检查把它变成机器可验证的。

判定：
- yaml 有、DEFAULTS 没有 → FAIL（代码/面板都不认这个键，写了等于没写）
- DEFAULTS 有、yaml 没有 → WARN（可以不写进示例，但建议写）
- 面板键表缺 → WARN（面板允许只显示一部分）
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

HERE = Path(__file__).resolve().parent
CODE = HERE.parent / "nonebot"
if not (CODE / "app_core").is_dir():
    CODE = HERE.parent

sys.path.insert(0, str(CODE))

import yaml  # noqa: E402

from app_core import config  # noqa: E402


def flatten(d: dict, prefix: str = "") -> set[str]:
    out: set[str] = set()
    for k, v in (d or {}).items():
        path = f"{prefix}{k}"
        if isinstance(v, dict) and v:
            out |= flatten(v, path + ".")
        else:
            out.add(path)
    return out


def main() -> int:
    defaults = flatten(config.DEFAULTS)
    yaml_path = CODE / "config.example.yaml"
    yaml_keys = flatten(yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {})
    ui_path = HERE / "config-ui.py"
    ui_keys = set()
    if ui_path.is_file():
        ui_keys = set(re.findall(r'"([a-z_]+(?:\.[a-z_0-9]+)+)"',
                                 ui_path.read_text(encoding="utf-8", errors="replace")))

    missing_default = sorted(yaml_keys - defaults)
    missing_yaml = sorted(defaults - yaml_keys)
    missing_ui = sorted(defaults - ui_keys)

    fails, warns = [], []
    for k in missing_default:
        fails.append(f"config.example.yaml 有 {k}，DEFAULTS 里没有——代码/面板不认这个键")
    for k in missing_yaml:
        warns.append(f"DEFAULTS 有 {k}，example.yaml 没写")
    for k in missing_ui:
        warns.append(f"DEFAULTS 有 {k}，面板键表没有")

    for w in warns:
        print("WARN", w)
    if fails:
        print("=== 配置漂移 ===")
        for f in fails:
            print("FAIL", f)
        return 1
    print(f"配置同步：PASS（DEFAULTS {len(defaults)} 键 / yaml {len(yaml_keys)} 键 / 面板 {len(ui_keys)} 键）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
