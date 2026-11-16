#!/usr/bin/env python3
"""[infra] 测试骨架自检：路径重定向清单是否覆盖所有会写盘的模块。

为什么需要：拆环/拆 Mixin 之后同一个路径常量在多个模块里各有一份，
测试漏补一处就会读写生产库（2026-11-16 一天踩了三次）。
ops/harness.py 的 redirect_paths() 是唯一清单，这里逐项断言它真的生效。
"""
from __future__ import annotations

from harness import bootstrap, check, redirect_paths, report, temp_root

bootstrap()

from app_core import (chat, files, followup_store, history, kb, learn,  # noqa: E402
                      learn_store, memory, mood_store, summary, wiki)
from app_core.scheduling import remind as sched_remind  # noqa: E402
from app_core.scheduling import scheduler as sched  # noqa: E402


def main() -> int:
    root = temp_root("harness-test-")
    done = redirect_paths(root)
    prefix = str(root)

    def under(p) -> bool:
        return str(p).startswith(prefix)

    for name, mod, attr in (
        ("chat", chat, "DB"), ("summary", summary, "DB"),
        ("history", history, "DB"), ("followup_store", followup_store, "DB"),
        ("kb", kb, "DB"), ("memory", memory, "DB"),
    ):
        check(f"{name}.DB 在临时根", under(getattr(mod, attr)), getattr(mod, attr))

    for attr in ("LEARN", "PROFILE", "PROFILE_JSON", "CORRECTIONS", "CURIOUS",
                 "SKILLS", "NOTES", "RAW", "MEM_DB", "CHAT_DB"):
        check(f"learn_store.{attr} 在临时根", under(getattr(learn_store, attr)),
              getattr(learn_store, attr))

    for attr in ("NOTES", "SKILLS", "RAW"):
        check(f"learn.{attr} 与 store 同源",
              getattr(learn, attr) == getattr(learn_store, attr))

    for attr in ("STATE", "STATE_DIR", "CHAT_DB"):
        check(f"mood_store.{attr} 在临时根", under(getattr(mood_store, attr)),
              getattr(mood_store, attr))

    check("scheduling.remind.DB 在临时根", under(sched_remind.DB), sched_remind.DB)
    check("scheduling.scheduler.CHAT_DB 在临时根", under(sched.CHAT_DB), sched.CHAT_DB)
    check("scheduling.scheduler.CLEANUP_LOG 在临时根", under(sched.CLEANUP_LOG))

    for attr in ("DB", "NOTES", "SKILLS"):
        check(f"wiki.{attr} 在临时根", under(getattr(wiki, attr)), getattr(wiki, attr))

    for attr in ("SAVE_DIR", "IMAGE_DIR", "PENDING_JSON"):
        check(f"files.{attr} 在临时根", under(getattr(files, attr)), getattr(files, attr))

    check("清单覆盖 10 个模块以上", len(done) >= 10, f"{len(done)} 个")
    return report()


if __name__ == "__main__":
    raise SystemExit(main())
