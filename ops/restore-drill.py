#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""restore-drill.py —— 每周真的把备份恢复一次，验证它能用。

**为什么需要**：`cloud-backup.sh` 只做 `tar -tzf`（能解压），没验证过「解出来能用」。
备份坏了、缺库、库是空的，这些事情只有真恢复才看得见。

做法：解到临时目录 → 按备份自带的 `manifest.json` 逐一核对文件（sha256 / 字节数）
→ 对每个必需库跑 `PRAGMA integrity_check` → 表行数与备份时一致 → 抽查代码文件
→ 写 `ops/restore.state`（健康检查会读它）。

**空库不一律算通过**：
- 清单里备份时就是 0 行 → `empty_by_design`（新装允许，日志里明说）；
- 备份时有数据、恢复后 0 行 → 失败；
- 旧备份没有 manifest → 严格模式：必需库和表必须存在且有行（无法区分空与坏）。

用法：
    ops/restore-drill.py                 # 用最新的 cloud-*.tgz
    ops/restore-drill.py /opt/backup/x.tgz
    ops/restore-drill.py --keep         # 保留临时目录（排障用）
"""
from __future__ import annotations

import hashlib
import json
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

# 必需库（备份内的相对路径）→ 必须存在的表。
# 路径与 cloud-backup.sh 的 staging 结构一致：/opt/<rel> → sqlite/<rel>。
REQUIRED_DBS = {
    "sqlite/qqbot-data/livingmemory.db": ("documents", "memory_atoms"),
    "sqlite/nonebot/data/chat.db": ("messages",),
}
# 代码文件抽检：备份里必须有，否则恢复出来起不来
CODE_FILES = ["code/app_core/chat.py", "code/plugins/chat/__init__.py"]
MANIFEST_FORMAT = 2


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


def check_db(path: Path, expected: dict | None = None) -> dict:
    """校验一个库。

    expected: 备份清单里的 {表名: 行数}；None 表示旧备份（严格模式）。
    返回 ok / integrity / tables / rows / empty_by_design / error。
    """
    out: dict = {"path": str(path), "ok": False, "tables": 0, "rows": {},
                 "empty_by_design": False, "errors": []}
    try:
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
        try:
            out["integrity"] = c.execute("PRAGMA integrity_check").fetchone()[0]
            names = [r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
            out["tables"] = len(names)
            want = expected
            if want is None:      # 旧备份：只要求主表存在且有行
                want = {}
                tables = (REQUIRED_DBS.get("sqlite/qqbot-data/" + path.name)
                          or REQUIRED_DBS.get("sqlite/nonebot/data/" + path.name)
                          or ())
                if tables:
                    want[tables[0]] = 1
            for t, n in want.items():
                if t not in names:
                    out["rows"][t] = -1
                    out["errors"].append(f"{path.name} 缺表 {t}")
                    continue
                got = c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                out["rows"][t] = got
                if got != int(n):
                    out["errors"].append(
                        f"{path.name}.{t} 行数 {got} != 备份时 {int(n)}")
            out["empty_by_design"] = bool(want) and all(
                int(n) == 0 for n in want.values())
        finally:
            c.close()
        if out.get("integrity") != "ok":
            out["errors"].append(f"{path.name} integrity_check = {out.get('integrity')}")
        if out["tables"] <= 0:
            out["errors"].append(f"{path.name} 没有任何表")
        out["ok"] = not out["errors"]
    except Exception as e:  # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {e}"
        out["errors"].append(out["error"])
    return out


def check_manifest(tmp: Path, manifest) -> list[str]:
    """清单里的每个文件都要在、字节数与 sha256 对得上；必需库必须在清单里。"""
    errors: list[str] = []
    if not isinstance(manifest, dict):
        return ["manifest.json 不是对象"]
    if manifest.get("format") != MANIFEST_FORMAT:
        return [f"manifest 版本不支持：{manifest.get('format')!r}"]
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        return ["manifest 没有 files 清单"]
    for rel, meta in files.items():
        p = tmp / rel
        if not p.is_file():
            errors.append(f"清单文件缺失：{rel}")
            continue
        blob = p.read_bytes()
        if len(blob) != int((meta or {}).get("bytes", -1)):
            errors.append(f"{rel} 字节数不符")
        if hashlib.sha256(blob).hexdigest() != (meta or {}).get("sha256"):
            errors.append(f"{rel} sha256 不符")
    dbs = manifest.get("dbs")
    if not isinstance(dbs, dict):
        return errors + ["manifest 没有 dbs 清单"]
    for rel in REQUIRED_DBS:
        if rel not in dbs:
            errors.append(f"清单缺必需库：{rel}")
    return errors


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
    result = {"ts": time.time(), "file": src.name, "ok": False, "dbs": [],
              "errors": [], "legacy": False}
    try:
        with tarfile.open(src, "r:gz") as tf:
            tf.extractall(tmp, filter="data")

        manifest = None
        mf = tmp / "manifest.json"
        if mf.is_file():
            try:
                manifest = json.loads(mf.read_text(encoding="utf-8"))
            except Exception as e:  # noqa: BLE001
                result["errors"].append(f"manifest.json 不可解析：{e}")
        else:
            # 旧备份没有清单：不能证明完整性，按严格模式（必需库 + 表非空）判。
            result["legacy"] = True
            result["errors"].append("备份缺 manifest.json（旧格式，无法核对文件完整性）")

        if manifest is not None:
            result["errors"].extend(check_manifest(tmp, manifest))
            result["dbs_manifest"] = {
                k: v.get("tables") for k, v in (manifest.get("dbs") or {}).items()
                if isinstance(v, dict)}

        for rel in REQUIRED_DBS:
            p = tmp / rel
            if not p.is_file():
                result["errors"].append(f"缺必需库 {rel}")
                continue
            expected = None
            if manifest is not None:
                meta = (manifest.get("dbs") or {}).get(rel) or {}
                tables = meta.get("tables")
                expected = tables if isinstance(tables, dict) else None
            info = check_db(p, expected)
            result["dbs"].append(info)
            if not info["ok"]:
                result["errors"].extend(info["errors"] or [f"{rel} 校验失败"])

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
        detail = "、".join(
            "%s %d 表/%s%s" % (Path(d["path"]).name, d["tables"], d["rows"],
                               "（空库按设计）" if d.get("empty_by_design") else "")
            for d in result["dbs"])
        log(f"恢复演练通过（{src.name}，{result['seconds']}s）：{detail}")
        return 0
    log(f"恢复演练失败（{src.name}）：" + "；".join(result["errors"])[:600])
    return 1


if __name__ == "__main__":
    sys.exit(main())
