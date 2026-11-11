"""无来源断言检测（只报告）。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

CODE = Path(os.environ.get("QQBOT_CODE_DIR", "/opt/nonebot")).resolve()
sys.path.insert(0, str(CODE))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

from app_core.fact_check import claims, unsourced  # noqa: E402

RESULTS: list[bool] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    RESULTS.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + name + (f" | {extra}" if extra else ""))


def main() -> None:
    check("抓百分比", "789%" in claims("新数据 789%"),
          str(claims("新数据 789%")))
    check("抓年月", any("2026" in c for c in claims("2026 年 9 月数据")))
    check("抓报告名", "《AI 人才市场报告》" in claims("《AI 人才市场报告》显示"))
    check("不抓普通数字", claims("三天后再说") == [], str(claims("三天后再说")))

    got = unsourced("科锐国际 9 月数据说 789%", "记忆里没有这些")
    check("无来源全部命中", len(got) >= 1, str(got))
    got2 = unsourced("789%", "新数据 789% 来自搜索")
    check("有来源不报", got2 == [], str(got2))

    passed = sum(1 for x in RESULTS if x)
    print(f"\n=== {passed}/{len(RESULTS)} 通过 ===")
    sys.exit(0 if passed == len(RESULTS) else 1)


if __name__ == "__main__":
    main()
