#!/usr/bin/env python3
"""[infra] 测试公共骨架：check / 汇总 / 路径注入 / 临时根 / DB 副本。

为什么抽：32 个 ops 脚本各自复制了一份 check()（合计 253 行），
外加各自的 PASS/FAIL 计数与结尾汇总。新增一条用例要先读一遍样板，
改一处输出格式要改 32 个文件。这里只保留一个实现，脚本只写用例。

用法：
    from harness import bootstrap, check, report
    bootstrap()              # 把 nonebot/ 注入 sys.path（必须在 import app_core 之前）
    check("名字", cond, extra)
    ...
    return report()          # 打印 "n/m 通过"，全过返回 0

注意：脚本直接运行时 sys.path[0] 就是 ops 目录，所以 `from harness import ...` 可用；
用 `python ops/test-xxx.py` 或云端 verify.sh 的调用方式都一样。
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
CODE = HERE.parent / "nonebot"
if not (CODE / "app_core").is_dir():
    CODE = HERE.parent

PASS = 0
FAIL = 0


def bootstrap() -> Path:
    """把代码根注入 sys.path，返回它（脚本里常要拼路径）。"""
    if str(CODE) not in sys.path:
        sys.path.insert(0, str(CODE))
    return CODE


def check(name, ok, extra="") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print("PASS " + name)
    else:
        FAIL += 1
        print("FAIL " + name + (" | " + str(extra) if extra else ""))


def report() -> int:
    print(f"\n=== {PASS}/{PASS + FAIL} 通过 ===")
    return 0 if FAIL == 0 else 1


def temp_root(prefix: str = "qqbot-test-") -> Path:
    """每次跑用独立临时根：离线测试不许碰生产库。"""
    return Path(tempfile.mkdtemp(prefix=prefix))


def copy_db(src, dst) -> None:
    """只读备份一个 sqlite 库到 dst；源不存在时留空文件（测试自己建表）。"""
    src, dst = Path(src), Path(dst)
    if not src.exists():
        dst.touch()
        return
    s = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    d = sqlite3.connect(dst)
    try:
        with d:
            s.backup(d)
    finally:
        s.close()
        d.close()
