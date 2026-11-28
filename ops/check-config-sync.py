#!/usr/bin/env python3
"""配置默认值与示例 YAML 同步检查。"""
from __future__ import annotations

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
    missing_default = sorted(yaml_keys - defaults)
    missing_yaml = sorted(defaults - yaml_keys)

    fails, warns = [], []
    for k in missing_default:
        fails.append(f"config.example.yaml 有 {k}，DEFAULTS 里没有——默认值未声明该键")
    for k in missing_yaml:
        warns.append(f"DEFAULTS 有 {k}，example.yaml 没写")

    for w in warns:
        print("WARN", w)
    if fails:
        print("=== 配置漂移 ===")
        for f in fails:
            print("FAIL", f)
        return 1
    print(f"配置同步：PASS（DEFAULTS {len(defaults)} 键 / yaml {len(yaml_keys)} 键）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
