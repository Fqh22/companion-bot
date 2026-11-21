#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""主体导出：把「它是谁」打成一份可读、可 diff、可版本化的文件集。

为什么：定义 01 §3 的 B 要求人格、记忆、关系状态能完整带走，且明确指出
「SQLite 里的记忆不算可携带」。仓库原来有 `export-memory.py`（只导两张表）
和 `export-persona.py`（只导人格），分散、无清单、无自校验，也不含画像 /
关系 / 纠正 / 跟进——换引擎时仍然不知道「还缺什么」。

本脚本把主体合成一个目录，并附 `manifest.json`（每份文件的 sha256 / 字节数
/ 条目数，以及每个源的状态）。所有 JSON 排序缩进、JSONL 每行一个稳定 dict、
统一 LF，放进 git 后 `git diff` 能直接看出「人格哪句改了、哪条记忆被取代了」。

**区分三种情况**（这是 2026-11-22 修的点）：
- `missing`：文件本来就没有（允许，显式记进清单）；
- `empty`：文件在、结构可读、就是 0 条（允许，记 0）；
- `failed`：文件在但读不了/损坏 → **导出失败**，清单里带 errors，
  `--verify` 也不会通过。以前损坏的 SQLite 被折叠成空数据，导出一份
  「校验通过的空快照」，等于把事故伪装成成功。

**刻意不导出**：
- 向量 blob：换 embedding 后本来就要重建（`ops/rebuild-vectors.py --apply`）；
- `keys.json` / `config.yaml`：凭证与部署配置不是主体，导出反而危险；
- 聊天历史：体量大且含隐私，主体是「人格 + 记忆 + 关系状态」，不是流水。

用法（云端）：
    /opt/nonebot/venv/bin/python ops/export-subject.py
    /opt/nonebot/venv/bin/python ops/export-subject.py --verify <导出的目录>

不写生产库：SQLite 一律 `mode=ro`，缺文件记为 `missing` 而不是报错退出。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

FORMAT_VERSION = 1
MEMORY_TABLES = ("documents", "memory_atoms")
REQUIRED_PARTS = ("README.md", "persona.json", "memory.jsonl", "profile.json",
                  "relations.md", "corrections.md", "followups.json")

README = """# companion 主体快照 · format_version {version}

这是「它是谁」的完整可携带副本，可直接放进 git 做版本管理。
改人格请改运行时的 `data/persona.json`，本目录是**单向快照**，不回写。

| 文件 | 内容 |
|---|---|
| `persona.json` | 人格本体：`system_prompt` + `begin_dialogs`，其余键原样保留 |
| `memory.jsonl` | 记忆：`documents` / `memory_atoms` 两张表，每行带 `_table` |
| `profile.json` | 画像字段与置信度 |
| `relations.md` | 关系小抄（长期关系状态） |
| `corrections.md` | 他纠正过的事 |
| `followups.json` | 还没跟进的话头 |
| `manifest.json` | 每份文件的 sha256 / 字节数 / 条目数，源状态与导出错误 |

**没导出的**：向量 blob（换 embedding 后跑 `ops/rebuild-vectors.py --apply` 重建）、
`keys.json` / `config.yaml`（凭证与部署配置）、聊天历史（体量与隐私）。

校验：`python ops/export-subject.py --verify <本目录>`
换引擎时按 `memory.jsonl` 的列名映射即可，不依赖本项目的 SQLite 结构。
"""


def _clean(v):
    """SQLite 值转 JSON：blob（向量）不导出，长得像 JSON 的字符串还原成对象。"""
    if isinstance(v, (bytes, bytearray)):
        return None
    if isinstance(v, str):
        s = v.strip()
        if s[:1] in "[{" and s[-1:] in "]}":
            try:
                return json.loads(s)
            except Exception:  # noqa: BLE001
                return v
    return v


def _dumps(obj) -> bytes:
    """稳定序列化：排序键 + 缩进 + 中文不转义 + LF，保证 diff 可读。"""
    return (json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=1) + "\n").encode("utf-8")


def _line(obj) -> bytes:
    return (json.dumps(obj, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")


def default_sources() -> dict[str, Path]:
    """生产路径；测试时用 `collect()` 注入临时目录。"""
    data = Path("/opt/nonebot/data")
    qdata = Path("/opt/qqbot-data")
    learn = qdata / "workspace" / "learn"
    return {
        "persona": data / "persona.json",
        "mem_db": qdata / "livingmemory.db",
        "chat_db": data / "chat.db",
        "profile": learn / "profile.json",
        "relations": data / "relation.md",
        "corrections": learn / "corrections.md",
    }


def _read_table(path: Path, table: str) -> tuple[list[dict], str]:
    """返回 (rows, state)。state: ok / empty / missing / missing_table。

    读失败**抛错**，不折叠成空列表——否则损坏的库和空库在清单里长得一样。
    """
    if not path.exists():
        return [], "missing"
    try:
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            cur = c.execute("select * from %s" % table)
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, [_clean(v) for v in row]))
                    for row in cur.fetchall()]
        finally:
            c.close()
    except sqlite3.OperationalError as e:
        if "no such table" in str(e).lower():
            return [], "missing_table"
        raise RuntimeError(f"{path.name}.{table} 读取失败：{e}") from e
    except sqlite3.Error as e:
        raise RuntimeError(f"{path.name}.{table} 读取失败：{e}") from e
    return rows, ("empty" if not rows else "ok")


def _read_json(path: Path) -> tuple[object, str]:
    """返回 (value, state)。文件在但 JSON 坏了 → 抛错，不当成 missing。"""
    if not path.exists():
        return None, "missing"
    try:
        return json.loads(path.read_text(encoding="utf-8")), "ok"
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"{path.name} 不是合法 JSON：{e}") from e


def _read_text(path: Path) -> tuple[bytes, str]:
    if not path.exists():
        return f"# missing: {path.name}\n".encode("utf-8"), "missing"
    try:
        return path.read_bytes(), "ok"
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"{path.name} 读取失败：{e}") from e


def _combine(states: list[str]) -> str:
    """一个源由多张表组成时的总状态：failed > ok > empty > missing。"""
    if "failed" in states:
        return "failed"
    if "ok" in states:
        return "ok"
    if "empty" in states:
        return "empty"
    return "missing"


def collect(sources: dict[str, Path], now: float | None = None) -> tuple[dict[str, bytes], dict]:
    """纯读，返回 `(parts, report)`；路径全部可注入，便于往返测试。

    report: {"counts": {...}, "sources": {...}, "errors": [...]}
    """
    stamp = datetime.fromtimestamp(now or datetime.now().timestamp()).isoformat(timespec="seconds")
    parts: dict[str, bytes] = {}
    counts: dict[str, int] = {}
    source_state: dict[str, str] = {}
    errors: list[str] = []

    try:
        persona, st = _read_json(sources["persona"])
        source_state["persona"] = st
        parts["persona.json"] = _dumps(persona if st != "missing" else {"missing": True})
    except Exception as e:  # noqa: BLE001
        source_state["persona"] = "failed"
        errors.append(str(e))
        parts["persona.json"] = _dumps({"error": str(e)})

    mem_lines: list[bytes] = []
    mem_states: list[str] = []
    for table in MEMORY_TABLES:
        try:
            rows, st = _read_table(sources["mem_db"], table)
        except Exception as e:  # noqa: BLE001
            source_state["mem_db"] = "failed"
            errors.append(str(e))
            rows, st = [], "failed"
        mem_states.append(st)
        counts[table] = len(rows)
        for r in rows:
            mem_lines.append(_line({"_table": table, **r}))
    if source_state.get("mem_db") != "failed":
        source_state["mem_db"] = _combine(mem_states)
    parts["memory.jsonl"] = b"".join(mem_lines)

    try:
        profile, st = _read_json(sources["profile"])
        source_state["profile"] = st
        parts["profile.json"] = _dumps(profile if st != "missing" else {"missing": True})
    except Exception as e:  # noqa: BLE001
        source_state["profile"] = "failed"
        errors.append(str(e))
        parts["profile.json"] = _dumps({"error": str(e)})

    for key, name in (("relations", "relations.md"), ("corrections", "corrections.md")):
        try:
            blob, st = _read_text(sources[key])
            source_state[key] = st
        except Exception as e:  # noqa: BLE001
            source_state[key] = "failed"
            errors.append(str(e))
            blob = f"# error: {sources[key].name}\n".encode("utf-8")
        parts[name] = blob

    try:
        follows, st = _read_table(sources["chat_db"], "followups")
        source_state["chat_db"] = st
    except Exception as e:  # noqa: BLE001
        follows, st = [], "failed"
        source_state["chat_db"] = "failed"
        errors.append(str(e))
    counts["followups"] = len(follows)
    parts["followups.json"] = _dumps(follows)

    parts["README.md"] = README.format(version=FORMAT_VERSION).encode("utf-8")

    manifest = {
        "format_version": FORMAT_VERSION,
        "exported_at": stamp,
        "counts": counts,
        "sources": source_state,
        "errors": errors,
        "parts": {
            name: {"sha256": hashlib.sha256(blob).hexdigest(), "bytes": len(blob)}
            for name, blob in sorted(parts.items())
        },
    }
    parts["manifest.json"] = _dumps(manifest)
    return parts, {"counts": counts, "sources": source_state, "errors": errors}


def verify(directory: Path) -> tuple[bool, list[str]]:
    """往返自检：清单里的每个文件都在、哈希与字节数都对、内容可解析。"""
    errors: list[str] = []
    mf = directory / "manifest.json"
    if not mf.exists():
        return False, ["缺 manifest.json"]
    try:
        manifest = json.loads(mf.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        return False, [f"manifest.json 不可解析：{e}"]
    if manifest.get("format_version") != FORMAT_VERSION:
        errors.append(f"format_version 不匹配：{manifest.get('format_version')}")
    # 导出时源读失败 → 这份快照不可信，不管文件本身多完整。
    src_errors = manifest.get("errors") or []
    if src_errors:
        errors.append("导出时源读取失败：" + "；".join(map(str, src_errors))[:300])
    for key, st in (manifest.get("sources") or {}).items():
        if st == "failed":
            errors.append(f"源 {key} 读取失败")
    parts_meta = manifest.get("parts") or {}
    for name in REQUIRED_PARTS:
        if name not in parts_meta:
            errors.append(f"清单缺分片：{name}")
    for name, meta in parts_meta.items():
        p = directory / name
        if not p.exists():
            errors.append(f"缺文件：{name}")
            continue
        blob = p.read_bytes()
        if len(blob) != meta.get("bytes"):
            errors.append(f"{name} 字节数不符：{len(blob)} != {meta.get('bytes')}")
        if hashlib.sha256(blob).hexdigest() != meta.get("sha256"):
            errors.append(f"{name} sha256 不符")
        if name.endswith(".json"):
            try:
                json.loads(blob.decode("utf-8"))
            except Exception as e:  # noqa: BLE001
                errors.append(f"{name} 不是合法 JSON：{e}")
        elif name.endswith(".jsonl"):
            for i, raw in enumerate(blob.decode("utf-8").splitlines(), 1):
                try:
                    row = json.loads(raw)
                except Exception as e:  # noqa: BLE001
                    errors.append(f"{name}:{i} 不是合法 JSON：{e}")
                    continue
                if "_table" not in row:
                    errors.append(f"{name}:{i} 缺 _table")
                elif row["_table"] == "documents" and not row.get("doc_id"):
                    errors.append(f"{name}:{i} documents 行缺 doc_id")
    # 条目数要和实际内容对得上：空快照即使清单自洽，也不能冒充「有数据」。
    counts = manifest.get("counts") or {}
    if "memory.jsonl" in parts_meta and "documents" in counts and "memory_atoms" in counts:
        n_lines = len((directory / "memory.jsonl").read_text(
            encoding="utf-8").splitlines())
        want = int(counts.get("documents") or 0) + int(counts.get("memory_atoms") or 0)
        if n_lines != want:
            errors.append(f"memory.jsonl 行数 {n_lines} != 清单计数 {want}")
    if "followups.json" in parts_meta and "followups" in counts:
        try:
            got = json.loads((directory / "followups.json").read_text(encoding="utf-8"))
            if not isinstance(got, list) or len(got) != int(counts.get("followups") or 0):
                errors.append("followups.json 条数与清单计数不符")
        except Exception as e:  # noqa: BLE001
            errors.append(f"followups.json 不可解析：{e}")
    return (not errors), errors


def main() -> int:
    ap = argparse.ArgumentParser(description="导出 / 校验 companion 主体快照")
    ap.add_argument("--out", type=Path,
                    default=Path("/opt/qqbot-data/workspace/subject"),
                    help="输出根目录；脚本在其下建 subject-YYYYmmdd-HHMMSS/")
    ap.add_argument("--verify", type=Path, help="校验一个已导出的目录，不写任何文件")
    args = ap.parse_args()

    if args.verify is not None:
        ok, errors = verify(args.verify)
        for e in errors:
            print("FAIL " + e)
        print("=== 校验通过：manifest 与所有分片一致 ===" if ok
              else f"=== 校验失败：{len(errors)} 项 ===")
        return 0 if ok else 1

    parts, report = collect(default_sources())
    target = args.out / ("subject-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
    target.mkdir(parents=True, exist_ok=True)
    for name, blob in parts.items():
        (target / name).write_bytes(blob)
    ok, errors = verify(target)
    for e in errors:
        print("FAIL " + e)
    if not ok or report["errors"]:
        print("=== 导出失败：源有读取错误，快照不可当作有效备份 ===")
        return 1
    print(f"ok: {target} | {len(parts)} files | counts={report['counts']} "
          f"| sources={report['sources']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
