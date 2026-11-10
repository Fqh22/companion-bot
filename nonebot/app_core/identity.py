"""身份映射：外部平台 ID -> 引擎内部 ID。

QQ 的内部 ID 继续用 QQ 号（存量数据零迁移）；新平台用 c-<uuid>。
映射表只是登记簿，核心逻辑只看 internal_id。
"""
from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path


class IdentityResolver:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.db_path, timeout=15)
        c.execute("PRAGMA journal_mode=WAL")
        return c

    def ensure(self) -> None:
        c = self._conn()
        with c:
            c.execute(
                "CREATE TABLE IF NOT EXISTS identity_map ("
                "platform TEXT NOT NULL,"
                "external_id TEXT NOT NULL,"
                "kind TEXT NOT NULL DEFAULT 'user',"
                "internal_id TEXT NOT NULL,"
                "created_ts REAL NOT NULL DEFAULT 0,"
                "PRIMARY KEY(platform, external_id, kind))")
            c.execute("CREATE INDEX IF NOT EXISTS idx_identity_internal "
                      "ON identity_map(internal_id)")
        c.close()

    def resolve(self, platform: str, external_id: str,
                kind: str = "user") -> str:
        """返回内部 ID；不存在就登记。QQ 直接用外部号，其他平台生成 c-<uuid>。"""
        p = str(platform or "").strip()
        e = str(external_id or "").strip()
        k = str(kind or "user").strip()
        if not p or not e:
            raise ValueError("platform/external_id 不能为空")
        self.ensure()
        c = self._conn()
        try:
            row = c.execute(
                "SELECT internal_id FROM identity_map "
                "WHERE platform=? AND external_id=? AND kind=?",
                (p, e, k)).fetchone()
            if row:
                return row[0]
            internal = e if p == "qq" else f"c-{uuid.uuid4().hex[:12]}"
            with c:
                c.execute(
                    "INSERT OR IGNORE INTO identity_map"
                    "(platform, external_id, kind, internal_id, created_ts) "
                    "VALUES (?,?,?,?,strftime('%s','now'))",
                    (p, e, k, internal))
            row = c.execute(
                "SELECT internal_id FROM identity_map "
                "WHERE platform=? AND external_id=? AND kind=?",
                (p, e, k)).fetchone()
            return row[0] if row else internal
        finally:
            c.close()

    def link(self, platform: str, external_id: str, internal_id: str,
             kind: str = "user") -> str:
        """把另一个外部身份绑到已有内部 ID（合并账号/多端同人）。"""
        self.ensure()
        c = self._conn()
        try:
            with c:
                c.execute(
                    "INSERT OR REPLACE INTO identity_map"
                    "(platform, external_id, kind, internal_id, created_ts) "
                    "VALUES (?,?,?,?,strftime('%s','now'))",
                    (platform, external_id, kind, internal_id))
        finally:
            c.close()
        return internal_id

    def internal(self, platform: str, external_id: str,
                 kind: str = "user") -> str | None:
        self.ensure()
        c = self._conn()
        try:
            row = c.execute(
                "SELECT internal_id FROM identity_map "
                "WHERE platform=? AND external_id=? AND kind=?",
                (platform, external_id, kind)).fetchone()
            return row[0] if row else None
        finally:
            c.close()
