"""对话规则：待办只在聊到相关词时注入（防翻旧账）。"""
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

from app_core.chat_turn import _topic_overlap  # noqa: E402

RESULTS: list[bool] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    RESULTS.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + name + (f" | {extra}" if extra else ""))


def main() -> None:
    check("相关词触发", _topic_overlap("填 base_url 和 key", "base_url 我填了"))
    check("无关短句不触发", not _topic_overlap("填 base_url 和 key", "算了先这样"))
    check("同主题触发", _topic_overlap("交房租", "房东催房租了"))
    check("空串不触发", not _topic_overlap("", "随便问问"))
    check("单字不误触发", not _topic_overlap("买猫粮", "好"))

    passed = sum(1 for x in RESULTS if x)
    print(f"\n=== {passed}/{len(RESULTS)} 通过 ===")
    sys.exit(0 if passed == len(RESULTS) else 1)


if __name__ == "__main__":
    main()
