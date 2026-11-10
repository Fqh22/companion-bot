#!/usr/bin/env python3
"""identity_map：QQ 零迁移、console 新 ID、link 合并。"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
CODE = HERE.parent / "nonebot"
if not (CODE / "app_core").is_dir():
    CODE = HERE.parent
sys.path.insert(0, str(CODE))

from app_core.identity import IdentityResolver  # noqa: E402

PASS = FAIL = 0


def check(name, ok, extra=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("PASS " + name)
    else:
        FAIL += 1
        print("FAIL " + name + (" | " + str(extra) if extra else ""))


def main():
    tmp = Path(tempfile.mkdtemp(prefix="identity-test-"))
    r = IdentityResolver(tmp / "chat.db")

    qq1 = r.resolve("qq", "599114669")
    qq2 = r.resolve("qq", "599114669")
    check("QQ 内部 ID 就是 QQ 号", qq1 == "599114669", qq1)
    check("QQ 重复解析稳定", qq2 == qq1, qq2)

    c1 = r.resolve("console", "local")
    c2 = r.resolve("console", "local")
    check("console 生成 c- 前缀", c1.startswith("c-"), c1)
    check("console 重复解析稳定", c2 == c1, c2)

    r.link("console", "local-2", c1)
    check("link 指向已有内部 ID", r.internal("console", "local-2") == c1,
          r.internal("console", "local-2"))
    check("未知身份返回 None", r.internal("qq", "404") is None)

    print(f"\n=== {PASS}/{PASS + FAIL} 通过 ===")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
