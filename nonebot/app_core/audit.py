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
import os
import time
import uuid
from pathlib import Path

from app_core import paths as _paths

AUDIT_LOG = _paths.DATA / "audit.jsonl"
PENDING_FILE = _paths.DATA / "audit-pending.json"
PENDING_TTL = 600.0            # 10 分钟：没人批准就作废
MAX_PENDING = 50

# 会改状态的工具。`web_open` 也在内：它会 learn.note_write 往知识库写笔记
# （以前被误列进只读白名单，见 tools.py 的注释）。
SIDE_EFFECT_TOOLS = frozenset({
    "note_write", "remind_me", "remind_cancel", "meme_send", "web_open",
})
# 消息一旦发出就撤不回，审计里必须明说「不可回滚」，不能假装能撤销。
IRREVERSIBLE = frozenset({"meme_send", "web_open"})


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


def record(action: str, *, target: str = "", actor: str = "model",
           status: str = "done", before: str = "", after: str = "",
           request_id: str = "", session: str = "", note: str = "",
           path: Path | None = None) -> dict:
    """追加一条审计记录。`status`: pending / done / approved / denied / expired / failed。"""
    log = Path(path) if path else AUDIT_LOG
    prev = _rows(log)
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
        "prev_hash": _line_hash(prev[-1]) if prev else "",
    }
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as f:
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


def requires_approval(name: str, cfg: dict | None = None) -> bool:
    """默认 False：不改现有行为。开启后只有写类工具需要授权。"""
    if name not in SIDE_EFFECT_TOOLS:
        return False
    if cfg is None:
        try:
            from app_core import config
            cfg = config.load()
        except Exception:  # noqa: BLE001
            return False
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
    items = {k: v for k, v in _load_pending(p).items()
             if now - float(v.get("ts") or 0) < PENDING_TTL}
    rid = uuid.uuid4().hex[:8]
    items[rid] = {"ts": now, "name": name,
                  "args": dict(args or {}), "session": session}
    # 只留最近 MAX_PENDING 条，别让没人管的请求一直堆着
    items = dict(sorted(items.items(), key=lambda kv: -float(kv[1].get("ts") or 0))
                 [:MAX_PENDING])
    try:
        _write(p, json.dumps(items, ensure_ascii=False, indent=1))
    except Exception:  # noqa: BLE001
        pass
    record(name, target=target_of(name, args), actor="model", status="pending",
           request_id=rid, session=session, path=log_path)
    return rid


def decide(request_id: str, approved: bool, by: str = "",
           pending_path: Path | None = None,
           log_path: Path | None = None) -> dict | None:
    """批准 / 拒绝；返回原来的请求（供调用方重放执行），过期或不存在的返回 None。"""
    p = Path(pending_path) if pending_path else PENDING_FILE
    items = _load_pending(p)
    item = items.pop(str(request_id or "").strip(), None)
    if item is None:
        return None
    status = "approved" if approved else "denied"
    if time.time() - float(item.get("ts") or 0) >= PENDING_TTL:
        status = "expired"
    try:
        _write(p, json.dumps(items, ensure_ascii=False, indent=1))
    except Exception:  # noqa: BLE001
        pass
    record(str(item.get("name") or ""), target=target_of(
        str(item.get("name") or ""), item.get("args") or {}),
        actor=by or "owner", status=status, request_id=str(request_id),
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
