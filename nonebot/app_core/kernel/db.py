"""SQLite 连接与事务入口：先把 WAL / busy timeout 收在一处。

现状：生产链路统一走 aiosqlite（chat.py / llm.py / scheduler 各自建短连接），
这个同步封装目前只被 ops/test-kernel.py 覆盖。保留是因为它把 WAL 和 busy_timeout
收在了一处；但**不要在 async 路径里新增同步调用**——会阻塞事件循环。
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


def connect(path: str | Path, timeout: float = 15.0) -> sqlite3.Connection:
    c = sqlite3.connect(str(path), timeout=timeout)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=15000")
    return c


@contextmanager
def transaction(path: str | Path) -> Iterator[sqlite3.Connection]:
    c = connect(path)
    try:
        with c:
            yield c
    finally:
        c.close()
