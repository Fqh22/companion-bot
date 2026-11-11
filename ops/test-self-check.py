"""回话自检标签：剥离与容错。"""
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

from app_core.self_check import looks_like_tag, parse  # noqa: E402

RESULTS: list[bool] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    RESULTS.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + name + (f" | {extra}" if extra else ""))


def main() -> None:
    tag, body = parse("[自检] 主=接住 次=判断 置信=高 | 依据=有 来源=记忆\n那就别撑着了。")
    check("剥离标签", tag.startswith("主=接住") and body == "那就别撑着了。", body[:20])
    check("没有标签原样返回", parse("直接说话") == ("", "直接说话"))
    check("空串不炸", parse("") == ("", ""))
    check("标签后有空行", parse("[自检] 主=判断\n\n正文")[1].startswith("正文"))
    tag2, body2 = parse("[自检] 主=事实 依据=有 来源=无  我只能发文字")
    check("标签后直接跟正文（无换行）", body2 == "我只能发文字", body2[:20])

    tag3, body3 = parse("[自检] 主=事实 次=判断 置信=高 | 依据=有 来源=记忆我这边收到的还是文字")
    check("来源后无空格也不吞正文", body3.startswith("我这边"), body3[:20])

    check("流式判断像标签", looks_like_tag("[自检] 主="))
    check("流式判断不像", not looks_like_tag("我觉得"))

    passed = sum(1 for x in RESULTS if x)
    print(f"\n=== {passed}/{len(RESULTS)} 通过 ===")
    sys.exit(0 if passed == len(RESULTS) else 1)


if __name__ == "__main__":
    main()
