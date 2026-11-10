"""产物清单：任务结束回传哪些文件、每个文件多大、sha 是什么。"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

MAX_BYTES = 20 * 1024 * 1024
MAX_FILES = 6


@dataclass(frozen=True)
class Artifact:
    path: str
    size: int
    sha256: str

    def as_dict(self) -> dict:
        return {"path": self.path, "size": self.size, "sha256": self.sha256}


def _sha(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 256), b""):
            h.update(chunk)
    return h.hexdigest()


def collect(work_dir: Path, started_at: float = 0) -> list[Artifact]:
    """任务开始后被创建/修改的文件；排除 PLAN 和超限文件。"""
    out: list[Artifact] = []
    try:
        for p in sorted(work_dir.iterdir()):
            if not p.is_file() or p.name == "PLAN.md":
                continue
            try:
                st = p.stat()
                if st.st_size > MAX_BYTES:
                    continue
                if started_at and st.st_mtime < started_at - 1:
                    continue
                out.append(Artifact(p.name, st.st_size, _sha(p)))
            except OSError:
                continue
            if len(out) >= MAX_FILES:
                break
    except OSError:
        pass
    return out


def paths(arts: list[Artifact], work_dir: Path) -> list[str]:
    return [str(work_dir / a.path) for a in arts]
