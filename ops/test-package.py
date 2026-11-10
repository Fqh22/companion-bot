#!/usr/bin/env python3
"""M6-mini：companion_core 门面、配置注入、包元数据。"""
from __future__ import annotations

import os
import sys
import tempfile
import tomllib
from pathlib import Path

HERE = Path(__file__).resolve().parent
CODE = HERE.parent / "nonebot"
if not (CODE / "app_core").is_dir():
    CODE = HERE.parent
sys.path.insert(0, str(CODE))

from companion_core import EngineConfig, __version__, create_engine  # noqa: E402
from app_core.engine import Engine  # noqa: E402

PASS = FAIL = 0


def check(name, ok, extra=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("PASS " + name)
    else:
        FAIL += 1
        print("FAIL " + name + (" | " + str(extra) if extra else ""))


class FakeCore:
    async def reply(self, *a, **k):
        return ["ok"], []


def main():
    check("版本号", __version__ == "0.1.0", __version__)
    check("pyproject 可解析", tomllib.loads(
        (HERE.parent / "pyproject.toml").read_text(encoding="utf-8"))["project"]["name"]
        == "companion-core")

    tmp = Path(tempfile.mkdtemp(prefix="core-config-"))
    old = os.environ.get("QQBOT_ROOT")
    try:
        cfg = EngineConfig(tmp)
        cfg.apply()
        check("EngineConfig 设置 QQBOT_ROOT", os.environ["QQBOT_ROOT"] == str(tmp.resolve()),
              os.environ.get("QQBOT_ROOT"))
        eng = create_engine(core=FakeCore())
        check("create_engine 返回 Engine", isinstance(eng, Engine), type(eng))
    finally:
        if old is None:
            os.environ.pop("QQBOT_ROOT", None)
        else:
            os.environ["QQBOT_ROOT"] = old

    check("memory_schema 在包内", (CODE / "app_core" / "memory_schema.sql").exists())

    print(f"\n=== {PASS}/{PASS + FAIL} 通过 ===")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
