"""长期维护：把长出来的东西收一收。

三件事，都不是「不做会立刻坏」，而是「做了一年省几十 MB、库还快」：

1. **JSONL 截断**：影子/矛盾日志只增不减。周报虽然只读尾部 4000 行，但文件本身
   会一直长；按天截断比按大小轮转简单，也不会把一次长会话切成两半。
2. **WAL checkpoint**：WAL 会把删掉的行留在 -wal 里，checkpoint 之后文件才会回去。
3. **VACUUM**：SQLite 删行不缩文件。prompt_log 每天删一百多条，不 VACUUM 就只涨不落。

放到每月 1 号 04:50 的 purge 后面跑：那时候备份刚做完，负载最低。
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

from app_core import paths as _paths

DATA = _paths.DATA
# 只收这些「追加式」日志。prompt_log 在库里，由 chat 自己截断。
LOG_GLOBS = ("shadow-*.jsonl", "contradictions.jsonl")
DBS = (DATA / "chat.db", _paths.MEM_DB)

# 单个文件超过这个大小就不读了（说明哪里出了问题，留给人工看，别把 04:50 拖成事故）
MAX_FILE_MB = 50


def trim_jsonl(path: Path, days: float = 30, now: float | None = None) -> dict:
    """按时间截断一个 jsonl。保留 ts 在窗口内的行；没有 ts 的行一并丢掉。"""
    now = time.time() if now is None else now
    cutoff = now - float(days) * 86400
    out = {"file": str(path), "before": 0, "after": 0, "kept": 0, "dropped": 0,
           "skipped": ""}
    try:
        if not path.exists():
            out["skipped"] = "not-exists"
            return out
        size = path.stat().st_size
        out["before"] = size
        if size > MAX_FILE_MB * 1024 * 1024:
            out["skipped"] = "too-large"
            out["after"] = size
            return out
        kept: list[str] = []
        dropped = 0
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                s = line.strip()
                if not s:
                    continue
                try:
                    rec = json.loads(s)
                    ts = float(rec.get("ts") or 0)
                except Exception:  # noqa: BLE001
                    dropped += 1
                    continue
                if ts >= cutoff:
                    kept.append(s)
                else:
                    dropped += 1
        if dropped:
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(chr(10).join(kept) + (chr(10) if kept else ""), encoding="utf-8")
            os.replace(tmp, path)
        out.update({"kept": len(kept), "dropped": dropped,
                    "after": path.stat().st_size})
    except Exception as e:  # noqa: BLE001
        out["skipped"] = f"{type(e).__name__}: {e}"
    return out


def vacuum(db_path: Path) -> dict:
    """checkpoint + VACUUM。返回前后的文件大小。"""
    out = {"db": str(db_path), "before": 0, "after": 0, "ok": False, "error": ""}
    try:
        if not db_path.exists():
            out["error"] = "not-exists"
            return out
        out["before"] = db_path.stat().st_size
        c = sqlite3.connect(str(db_path), timeout=30)
        try:
            c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            c.execute("VACUUM")
        finally:
            c.close()
        out["after"] = db_path.stat().st_size
        out["ok"] = True
    except Exception as e:  # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {e}"
    return out


OPS = _paths.NB / "ops"


def _read_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def ops_status(ops_dir: Path | None = None, now: float | None = None) -> list[str]:
    """运维两件事的现状，进周报：异地备份、恢复演练。

    放在周报而不是 /ping：这两个是「周级」的事，天天提示会变成噪音；
    但一直不提示，就会变成「备份从来没验证过」。
    """
    now = time.time() if now is None else now
    d = Path(ops_dir) if ops_dir else OPS
    out: list[str] = []

    off = _read_state(d / "offsite.state")
    st = str(off.get("status") or "")
    if st == "ok":
        age = max(0.0, (now - float(off.get("ts") or 0)) / 3600.0)
        out.append("异地备份：最近成功 %.0f 小时前（%s）" % (age, off.get("file") or "?"))
    elif st == "failed":
        out.append("异地备份：**上次失败**，需要看一眼 ops/offsite.log")
    elif st == "unconfigured":
        out.append("异地备份：**未配置**——备份和数据在同一块盘，盘挂了会一起没")
    else:
        out.append("异地备份：**从未跑过**")

    rs = _read_state(d / "restore.state")
    if rs:
        age_d = max(0.0, (now - float(rs.get("ts") or 0)) / 86400.0)
        if not rs.get("ok"):
            out.append("恢复演练：**上次失败**（%s）" % "；".join(rs.get("errors") or []))
        elif age_d > 8:
            out.append("恢复演练：已经 %.0f 天没跑了" % age_d)
        else:
            out.append("恢复演练：%.0f 天前通过（%d 个库）"
                       % (age_d, len(rs.get("dbs") or [])))
    else:
        out.append("恢复演练：**从未做过**——备份能不能用还是未知数")
    return out


def run(days: float = 30, now: float | None = None,
        data_dir: Path | None = None, dbs=None) -> dict:
    """跑一遍全部维护。返回一份可记日志的账。"""
    root = Path(data_dir) if data_dir else DATA
    logs: list[dict] = []
    for pat in LOG_GLOBS:
        for p in sorted(root.glob(pat)):
            logs.append(trim_jsonl(p, days, now))
    dbs_out = [vacuum(p) for p in (dbs if dbs is not None else DBS)]
    return {
        "logs": {
            "files": len(logs),
            "dropped": sum(x.get("dropped") or 0 for x in logs),
            "freed_kb": max(0, sum((x.get("before") or 0) - (x.get("after") or 0)
                                   for x in logs) // 1024),
            "details": [x for x in logs if x.get("dropped") or x.get("skipped")],
        },
        "dbs": dbs_out,
    }
