#!/usr/bin/env python3
"""M6-mini：companion_core 门面、配置注入、包元数据。"""
from __future__ import annotations

import os
import asyncio
import sqlite3
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

HERE = Path(__file__).resolve().parent
CODE = HERE.parent / "nonebot"
if not (CODE / "app_core").is_dir():
    CODE = HERE.parent
sys.path.insert(0, str(CODE))

from companion_core import EngineConfig, __version__, create_application, create_engine  # noqa: E402
from app_core.engine import Engine  # noqa: E402

from harness import check, report


class FakeCore:
    async def reply(self, *a, **k):
        return ["ok"], []


class StartupCore(FakeCore):
    initialized = 0

    async def init_db(self):
        self.initialized += 1
        await asyncio.sleep(0)

    def config_issues(self):
        return []

    def config_status(self):
        return "test"


async def check_startup():
    core = StartupCore()
    app = create_application(core=core)
    await asyncio.gather(app.start(), app.start())
    check("并发启动只初始化一次", core.initialized == 1)
    await app.stop()


def check_roots(tmp: Path):
    script = """
import asyncio, sys
from pathlib import Path
from companion_core import EngineConfig, create_application
async def main():
    app = create_application(EngineConfig(Path(sys.argv[1])))
    await app.start()
    assert await app.core.record_delivery("private:test", sys.argv[2])
    await app.stop()
    try:
        EngineConfig(Path(sys.argv[1]) / "other").apply()
    except RuntimeError:
        return
    raise AssertionError("已启动的进程不能悄悄改用另一份库")
asyncio.run(main())
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(filter(None, (str(CODE), env.get("PYTHONPATH"))))
    procs = []
    for marker in ("alpha", "beta"):
        target = tmp / marker
        done = subprocess.run([sys.executable, "-B", "-c", script, str(target), marker],
                              env=env, capture_output=True, text=True, timeout=30)
        if done.returncode:
            check("独立数据根应用启动 " + marker, False, done.stderr[-1200:])
            continue
        conn = sqlite3.connect(target / "nonebot/data/chat.db")
        try:
            procs.append(conn.execute("SELECT content FROM messages").fetchall())
        finally:
            conn.close()
    check("两个进程各写自己的数据根", procs == [[("alpha",)], [("beta",)]], procs)


def main():
    check("版本号", __version__ == "0.1.0", __version__)
    pyproject = HERE.parent / "pyproject.toml"
    if pyproject.exists():
        check("pyproject 可解析", tomllib.loads(
            pyproject.read_text(encoding="utf-8"))["project"]["name"] == "companion-core")
    else:
        # 云端部署只带 nonebot/ + ops/，仓库根的 pyproject 不在部署范围内；
        # CI 在仓库根跑，这条仍然生效。
        print("SKIP pyproject（部署目录不带仓库根）")

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

    asyncio.run(check_startup())
    check_roots(tmp)

    check("memory_schema 在包内", (CODE / "app_core" / "memory_schema.sql").exists())

    return report()


if __name__ == "__main__":
    raise SystemExit(main())
