"""[infra] 统一告警面：把「静默失败」变成「可数的失败」。

为什么要它：项目里的失败分支几乎全是 log.debug——生产环境看不到，
也没有人天天翻 journalctl。2026-11-15 凌晨智谱欠费，写记忆失败了一整晚，
直到用户发语音才发现。日志不是告警，日志是给已经在找问题的人看的。

设计约束：
- 本模块**永不抛异常**：告警失败不该让业务也跟着崩；
- 同一 code 60 秒内只记一条（否则每轮失败会刷爆）；
- 文件超过 1MB 自动截断保留最后 500 条；
- 只写文件 + log.warning，不引入新的外部依赖。
"""
from __future__ import annotations

import json
import logging
import time

from app_core import paths

log = logging.getLogger("qqbot.alert")

LOG = paths.DATA / "alerts.jsonl"
MAX_BYTES = 1_000_000
THROTTLE = 60.0
_last: dict[str, float] = {}
_writes = 0


def record(level: str, code: str, detail: str = "", session: str = "") -> None:
    """记一条告警。level: info / warn / error。永不抛异常。"""
    global _writes
    try:
        now = time.time()
        if now - _last.get(code, 0.0) < THROTTLE:
            return
        _last[code] = now
        line = json.dumps({"ts": now, "level": level, "code": code,
                           "detail": str(detail)[:300], "session": str(session)[:40]},
                          ensure_ascii=False)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        if level in ("warn", "error"):
            log.warning("[alert] %s %s", code, str(detail)[:120])
        _writes += 1
        if _writes % 50 == 1:
            _rotate()
    except Exception:  # noqa: BLE001
        pass


def _rotate() -> None:
    try:
        if LOG.exists() and LOG.stat().st_size > MAX_BYTES:
            lines = LOG.read_text(encoding="utf-8", errors="replace").splitlines()[-500:]
            LOG.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def recent(hours: float = 24.0) -> dict:
    """最近 N 小时的计数：{code: n}。给 /status 和周报用。"""
    out: dict[str, int] = {}
    try:
        if not LOG.exists():
            return out
        cutoff = time.time() - hours * 3600
        for line in LOG.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                row = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            if float(row.get("ts") or 0) >= cutoff:
                k = str(row.get("code") or "?")
                out[k] = out.get(k, 0) + 1
    except Exception:  # noqa: BLE001
        pass
    return out
