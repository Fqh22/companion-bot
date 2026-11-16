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


def redirect_paths(root, chat_db: str = "chat.db") -> dict[str, list[str]]:
    """把「会写盘」的模块路径常量统一指到 root 下，返回 {模块: [改过的属性]}。

    为什么需要这张清单：拆环 / 拆 Mixin 之后，同一个路径常量会在多个模块里各有一份——
    chat.DB、summary.DB、history.DB、learn_store.LEARN、mood_store.STATE …… 
    测试只补其中一处，另几处仍然指向默认根（线上就是生产库）。
    2026-11-16 一天里因此踩了三次。新测试**只调这个函数**，不要再手写补丁点。

    调用时机：import app_core 之后、跑用例之前。默认根是 /opt，所以漏掉任何一处
    都可能读写生产数据——这也是 ops/test-harness.py 会逐项断言的原因。
    """
    from pathlib import Path as _P

    from app_core import (chat, files, followup_store, history, kb, learn,
                          learn_store, memory, mood_store, summary, wiki)
    from app_core.scheduling import remind as sched_remind
    from app_core.scheduling import scheduler as sched

    root = _P(root)
    root.mkdir(parents=True, exist_ok=True)
    db = root / chat_db
    learn_root = root / "learn"
    done: dict[str, list[str]] = {}

    def setp(mod, name, value):
        setattr(mod, name, value)
        done.setdefault(mod.__name__.rsplit(".", 1)[-1], []).append(name)

    # 对话库：chat / summary / history / followup_store / kb / memory 各有一份常量
    for mod in (chat, summary, history, followup_store, kb, memory):
        setp(mod, "DB", db)
    setp(chat, "DATA", root)

    # 学习层的存储（learn_store 是真相源，learn 只是 re-export 旧入口）
    for name, value in (
        ("LEARN", learn_root),
        ("PROFILE", learn_root / "self_profile.md"),
        ("PROFILE_JSON", learn_root / "profile.json"),
        ("CORRECTIONS", learn_root / "corrections.md"),
        ("CURIOUS", learn_root / "curious.md"),
        ("SKILLS", learn_root / "skills"),
        ("NOTES", learn_root / "notes"),
        ("RAW", learn_root / "raw"),
        ("MEM_DB", root / "livingmemory.db"),
        ("CHAT_DB", db),
    ):
        setp(learn_store, name, value)
    # learn 保留的函数（note_write / skill_* / archive_raw）读自己的全局
    for name in ("NOTES", "SKILLS", "RAW", "CHAT_DB", "MEM_DB"):
        if hasattr(learn, name):
            setp(learn, name, getattr(learn_store, name))

    # 情绪：状态文件与历史表
    setp(mood_store, "STATE_DIR", root / "mood")
    setp(mood_store, "STATE", root / "mood" / "state.json")
    setp(mood_store, "CHAT_DB", db)

    # 定时与提醒
    setp(sched_remind, "DB", db)
    setp(sched, "CHAT_DB", db)
    setp(sched, "CLEANUP_LOG", root / "memory-cleanup.log")

    # 知识索引
    setp(wiki, "DB", db)
    setp(wiki, "NOTES", learn_root / "notes")
    setp(wiki, "SKILLS", learn_root / "skills")

    # 文件与图片
    setp(files, "SAVE_DIR", root / "files")
    setp(files, "IMAGE_DIR", root / "files" / "images")
    setp(files, "PENDING_JSON", root / "last-file.json")

    # 选配路径可能不存在（老版本模块）：只记录改到的
    return done

