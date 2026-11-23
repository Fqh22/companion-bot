"""[infra] 在途轮次标记：发布脚本据此判断能不能停服务。

聊天库里的用户消息要到生成结束后才落库。只看「最后一条 user/assistant
时间」会把正在生成的这一轮当成已经结束。reply 开始时写一行，结束时删掉。
进程崩溃留下的标记用 pid 是否存活判断，避免永远占着部署。
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from app_core import paths

_LOCK = threading.Lock()
MAX_AGE = 180.0


def path() -> Path:
    return paths.DATA / "inflight.json"


def _read(p: Path) -> dict:
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}


def _write(p: Path, data: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)


def mark(session: str) -> None:
    """记一笔正在生成的轮次。写失败不能打断回复。"""
    sid = str(session or "")
    if not sid:
        return
    try:
        p = path()
        with _LOCK:
            data = _read(p)
            data[sid] = {"pid": os.getpid(), "ts": time.time()}
            _write(p, data)
    except Exception:  # noqa: BLE001
        return


def clear(session: str) -> None:
    """这一轮结束（成功、失败或取消）后清掉。"""
    sid = str(session or "")
    if not sid:
        return
    try:
        p = path()
        with _LOCK:
            data = _read(p)
            data.pop(sid, None)
            if data:
                _write(p, data)
            elif p.exists():
                p.unlink()
    except Exception:  # noqa: BLE001
        return


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def busy(session: str | None = None, max_age: float = MAX_AGE) -> bool:
    """有没有还在生成的非群聊轮次。死进程或超时的标记不算。"""
    now = time.time()
    for sid, rec in _read(path()).items():
        if session is not None and sid != session:
            continue
        if str(sid).startswith("group:"):
            continue
        if not isinstance(rec, dict):
            continue
        age = now - float(rec.get("ts") or 0)
        if age >= max_age:
            continue
        if _alive(int(rec.get("pid") or 0)):
            return True
    return False
