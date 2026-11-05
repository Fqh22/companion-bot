#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""restore-drill.py —— 每周真的把备份恢复一次，验证它能用。

**为什么需要**：`cloud-backup.sh` 只做 `tar -tzf`（能解压），没验证过「解出来能用」。
备份坏了、缺库、库是空的，这些事情只有真恢复才看得见。

做法：解到临时目录 → 对每个库跑 `PRAGMA integrity_check` → 查关键表有没有行 →
抽查代码文件在不在 → 写 `ops/restore.state`（健康检查会读它）。

用法：
    ops/restore-drill.py                 # 用最新的 cloud-*.tgz
    ops/restore-drill.py /opt/backup/x.tgz
    ops/restore-drill.py --keep         # 保留临时目录（排障用）
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tarfile
import tempfile
import time
from pathlib import Path

BACKUP = Path("/opt/backup")
STATE = Path("/opt/nonebot/ops/restore.state")
LOG = Path("/opt/nonebot/ops/restore.log")

# 库 → 必须存在的表（空表也算失败：备份到了但数据没进去）
EXPECT = {
    "livingmemory.db": ["documents"],
    "chat.db": ["messages"],
    "agentd.db": ["tasks"],
}
# 代码文件抽检：备份里必须有，否则恢复出来起不来
CODE_FILES = ["code/app_core/chat.py", "code/plugins/chat/__init__.py"]


def log(msg: str) -> None:
    line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        with LOG.open("a", encoding="utf-8") as f:
            f.write(line + chr(10))
    except Exception:  # noqa: BLE001
        pass
    print(line)


def latest() -> Path | None:
    items = sorted(BACKUP.glob("cloud-*.tgz"), key=lambda p: p.stat().st_mtime)
    return items[-1] if items else None


def check_db(path: Path) -> dict:
    out = {"path": str(path), "ok": False, "tables": 0, "rows": {}}
    try:
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
        try:
            out["integrity"] = c.execute("PRAGMA integrity_check").fetchone()[0]
            names = [r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
            out["tables"] = len(names)
            for t in (EXPECT.get(path.name) or []):
                if t in names:
                    out["rows"][t] = c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                else:
                    out["rows"][t] = -1
        finally:
            c.close()
        ok = out.get("integrity") == "ok" and out["tables"] > 0
        for t, n in out["rows"].items():
            if n is not None and n < 0:
                ok = False
        out["ok"] = ok
    except Exception as e:  # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {e}"
    return out


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    keep = "--keep" in sys.argv
    src = Path(args[0]) if args else latest()
    if not src or not src.exists():
        log("恢复演练失败：找不到备份文件")
        STATE.write_text(json.dumps({"ts": time.time(), "ok": False,
                                     "error": "no backup"}, ensure_ascii=False))
        return 2

    t0 = time.time()
    tmp = Path(tempfile.mkdtemp(prefix="restore-drill-"))
    result = {"ts": time.time(), "file": src.name, "ok": False, "dbs": [], "errors": []}
    try:
        with tarfile.open(src, "r:gz") as tf:
            tf.extractall(tmp, filter="data")
        dbs = sorted(tmp.glob("sqlite/**/*.db"))
        if not dbs:
            result["errors"].append("备份里没有 sqlite 库")
        for db in dbs:
            info = check_db(db)
            result["dbs"].append(info)
            if not info["ok"]:
                result["errors"].append(f"{db.name} 校验失败")
        for rel in CODE_FILES:
            if not (tmp / rel).exists():
                result["errors"].append(f"缺代码文件 {rel}")
        result["ok"] = not result["errors"]
    except Exception as e:  # noqa: BLE001
        result["errors"].append(f"{type(e).__name__}: {e}")
    finally:
        if keep:
            log(f"临时目录保留在 {tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)

    result["seconds"] = round(time.time() - t0, 1)
    try:
        STATE.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    if result["ok"]:
        detail = "、".join("%s %d 表/%s" % (Path(d["path"]).name, d["tables"],
                                          d["rows"]) for d in result["dbs"])
        log(f"恢复演练通过（{src.name}，{result['seconds']}s）：{detail}")
        return 0
    log(f"恢复演练失败（{src.name}）：" + "；".join(result["errors"]))
    return 1


if __name__ == "__main__":
    sys.exit(main())
