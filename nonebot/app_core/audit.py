"""[orchestration] 副作用审计：每次「动了东西」都留下可追溯、可回滚的记录。

定义 01 §3 的 C 说「能办小事，但每次副作用可追溯、可回滚、需授权」，
11-10 删掉 agentd 时留下的前提是「先有审计层」。核对后发现这个前提**一直没兑现**：
`approval.py` 只是内存 Future（无落盘、无超时、无队列），`tool_usage.jsonl`
只有工具名和成败，回答不了「谁让它写的、之前是什么、能不能撤销」。

这里补的是地基，不是新的自主执行能力：
- 只记**已发生 / 待授权**的副作用，纯读工具不记；
- 状态变化**追加新行**，不覆盖旧记录，天然构成时间线；
- 每行带 `prev_hash`，中间被改会让后面全对不上（`verify_chain`）；
- 可选授权门 `tools.require_approval`（默认关）：开启时写类工具先落
  `pending`，主人用 `/approve <id>` 批准后才重放执行；不发就自动过期。

不做无人监督的自主执行：门一开，模型自己不能悄悄写入。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
import uuid
from pathlib import Path

from app_core import paths as _paths
from app_core.tool_policy import IRREVERSIBLE, SIDE_EFFECT_TOOLS

log = logging.getLogger("qqbot.audit")

AUDIT_LOG = _paths.DATA / "audit.jsonl"
PENDING_FILE = _paths.DATA / "audit-pending.json"
PENDING_TTL = 600.0            # 10 分钟：没人批准就作废
MAX_PENDING = 50


def _dump(rec: dict) -> str:
    return json.dumps(rec, ensure_ascii=False, sort_keys=True)


def _line_hash(rec: dict) -> str:
    return hashlib.sha256(_dump(rec).encode("utf-8")).hexdigest()[:16]


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except Exception:  # noqa: BLE001  Windows 上不起作用，忽略
        pass


def _rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            r = json.loads(raw)
        except Exception:  # noqa: BLE001
            continue
        if isinstance(r, dict):
            out.append(r)
    return out


_LOG_LOCK = threading.Lock()


def _last_row(path: Path) -> dict | None:
    """从文件末尾读取最后一条合法 JSON 行，避免每次写入全表扫描。"""
    if not path.exists():
        return None
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            if size <= 0:
                return None
            f.seek(max(0, size - 16384))
            chunk = f.read().decode("utf-8", errors="replace")
        for raw in reversed(chunk.splitlines()):
            raw = raw.strip()
            if not raw:
                continue
            try:
                r = json.loads(raw)
                if isinstance(r, dict):
                    return r
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        pass
    rows = _rows(path)
    return rows[-1] if rows else None


def record(action: str, *, target: str = "", actor: str = "model",
           status: str = "done", before: str = "", after: str = "",
           request_id: str = "", session: str = "", note: str = "",
           path: Path | None = None) -> dict:
    """追加一条审计记录。`status`: pending / done / approved / denied / expired / failed。"""
    log_path = Path(path) if path else AUDIT_LOG
    with _LOG_LOCK:
        prev = _last_row(log_path)
        rec = {
            "ts": time.time(),
            "request_id": request_id or uuid.uuid4().hex[:12],
            "action": action,
            "target": str(target)[:120],
            "actor": actor,
            "status": status,
            "before": str(before)[:200],
            "after": str(after)[:200],
            "note": str(note)[:120],
            "session": str(session)[:60],
            "revertible": action not in IRREVERSIBLE,
            "prev_hash": _line_hash(prev) if prev else "",
        }
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as f:
                f.write(_dump(rec) + "\n")
        except Exception:  # noqa: BLE001  审计失败不该让主流程挂掉
            pass
    return rec


def recent(n: int = 8, path: Path | None = None) -> list[dict]:
    return _rows(Path(path) if path else AUDIT_LOG)[-max(0, int(n)):]


def verify_chain(rows: list[dict]) -> tuple[bool, list[str]]:
    """校验相邻两行的哈希链；被篡改 / 被删行会在这里露出来。"""
    errors = []
    for i in range(1, len(rows)):
        want = _line_hash(rows[i - 1])
        if rows[i].get("prev_hash") != want:
            errors.append(f"第 {i} 行 prev_hash 对不上（前一行可能被改过）")
    return (not errors), errors


class ApprovalPolicyError(RuntimeError):
    """审批策略读不到。不能当成「已关闭」，否则写操作会绕过授权门。"""


def requires_approval(name: str, cfg: dict | None = None) -> bool:
    """默认 False：不改现有行为。开启后只有写类工具需要授权。

    「明确关闭」和「读不到策略」不是一回事。配置损坏或读取抛错时抛
    ApprovalPolicyError，调用方必须暂停受控写操作，不能按关闭处理。
    调用方自己传入 cfg 时，以这份为准（测试和重放）。
    """
    if name not in SIDE_EFFECT_TOOLS:
        return False
    if cfg is None:
        from app_core import config
        try:
            cfg = config.load()
        except Exception as e:  # noqa: BLE001
            raise ApprovalPolicyError(f"无法读取审批策略：{e}") from e
        err = config.load_error()
        if err:
            raise ApprovalPolicyError(f"审批策略无法读取（配置损坏）：{err}")
    return bool((cfg.get("tools") or {}).get("require_approval", False))


def _load_pending(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def propose(name: str, args: dict, session: str = "",
            pending_path: Path | None = None,
            log_path: Path | None = None) -> str:
    """登记一个待授权请求，返回 `/approve` 用的 id。"""
    p = Path(pending_path) if pending_path else PENDING_FILE
    now = time.time()
    # 与 decide 共用一把锁：并发 propose 会互相覆盖待授权条目。
    with _DECIDE_LOCK:
        items = {k: v for k, v in _load_pending(p).items()
                 if now - float(v.get("ts") or 0) < PENDING_TTL}
        rid = uuid.uuid4().hex[:8]
        items[rid] = {"ts": now, "name": name,
                      "args": dict(args or {}), "session": session}
        # 只留最近 MAX_PENDING 条，别让没人管的请求一直堆着
        items = dict(sorted(items.items(), key=lambda kv: -float(kv[1].get("ts") or 0))
                     [:MAX_PENDING])
        # 登记必须落盘成功才算数：落盘失败还返回 id，主人会拿到一个不存在的
        # 授权号。让异常上抛，由 tools.execute 拒绝本次写操作。
        _write(p, json.dumps(items, ensure_ascii=False, indent=1))
    record(name, target=target_of(name, args), actor="model", status="pending",
           request_id=rid, session=session, path=log_path)
    return rid


# 同一进程内的批准串行：两个 /approve 同时读同一份 pending 再各自写回，
# 后写的会覆盖先写的，两边都拿到可执行请求 → 同一 ID 执行两次。
# 跨进程不共享这把锁，但落盘仍是"先写后执行"的原子领取。
_DECIDE_LOCK = threading.Lock()


def decide(request_id: str, approved: bool, by: str = "",
           pending_path: Path | None = None,
           log_path: Path | None = None) -> dict | None:
    """批准 / 拒绝；返回原来的请求（供调用方重放执行），过期或不存在的返回 None。

    **先原子领取再执行**：把请求从 pending 里删掉并落盘成功之后才返回 item。
    落盘失败就返回 None——调用方不会执行；同一 ID 也不会因为"删失败但仍返回"
    被重放。返回之后即使执行失败，请求也不会回到 pending（at-most-once）。
    """
    p = Path(pending_path) if pending_path else PENDING_FILE
    rid = str(request_id or "").strip()
    with _DECIDE_LOCK:
        items = _load_pending(p)
        item = items.pop(rid, None)
        if item is None:
            return None
        status = "approved" if approved else "denied"
        if time.time() - float(item.get("ts") or 0) >= PENDING_TTL:
            status = "expired"
        try:
            _write(p, json.dumps(items, ensure_ascii=False, indent=1))
        except Exception as e:  # noqa: BLE001
            # 没删掉就不能执行：否则下次批准还能再执行一遍。
            log.error("待授权记录落盘失败，拒绝执行 %s：%s", rid, e)
            return None
        record(str(item.get("name") or ""), target=target_of(
            str(item.get("name") or ""), item.get("args") or {}),
            actor=by or "owner", status=status, request_id=rid,
            session=str(item.get("session") or ""), path=log_path)
    return item if status == "approved" else None


def target_of(name: str, args: dict) -> str:
    """审计里的「动了哪个对象」：只取标识性字段，不把整段正文塞进日志。"""
    a = args or {}
    for key in ("title", "path", "text", "query", "url", "topic", "id"):
        v = a.get(key)
        if v:
            return str(v)[:120]
    return ""


def pending_ids(pending_path: Path | None = None) -> list[str]:
    p = Path(pending_path) if pending_path else PENDING_FILE
    now = time.time()
    return [k for k, v in _load_pending(p).items()
            if now - float(v.get("ts") or 0) < PENDING_TTL]
