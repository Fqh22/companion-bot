-- 长期记忆库表结构（/opt/qqbot-data/livingmemory.db）
--
-- 来源：2026-10-31 从生产库 `sqlite3 .schema` 导出。
-- 原先由 AstrBot 的 livingmemory 插件创建，该插件已随 AstrBot 一并删除，
-- 这份 DDL 是重建记忆库的唯一依据，改动前请先确认 memory.py 的读写字段。
--
-- 重建方式：
--   sqlite3 /opt/qqbot-data/livingmemory.db < memory_schema.sql
-- 注意：memory_vectors 由 app_core/memory.py 的 init() 建，这里一并列出以便完整重建。

-- ---------- documents：一条长期记忆 ----------
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER NOT NULL,
    doc_id VARCHAR NOT NULL,
    text VARCHAR NOT NULL,
    metadata TEXT,
    created_at DATETIME,
    updated_at DATETIME,
    PRIMARY KEY (id),
    UNIQUE (doc_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_documents_doc_id_unique ON documents(doc_id);

-- 召回/清理按 metadata 里的字段过滤和排序，这些表达式索引必须与查询字面量一致
CREATE INDEX IF NOT EXISTS idx_doc_metadata
    ON documents(json_extract(metadata, '$.session_id'));
CREATE INDEX IF NOT EXISTS idx_doc_persona_metadata
    ON documents(json_extract(metadata, '$.persona_id'));
CREATE INDEX IF NOT EXISTS idx_doc_importance_metadata
    ON documents(json_extract(metadata, '$.importance'));
CREATE INDEX IF NOT EXISTS idx_doc_last_access_metadata
    ON documents(json_extract(metadata, '$.last_access_time'));
CREATE INDEX IF NOT EXISTS idx_doc_status
    ON documents(COALESCE(json_extract(metadata, '$.status'), 'active'));
CREATE INDEX IF NOT EXISTS idx_doc_create_time
    ON documents(
        COALESCE(CAST(json_extract(metadata, '$.create_time') AS REAL), 0) DESC,
        id DESC
    );

-- ---------- memory_atoms：从记忆里拆出的原子事实，供间隔重复使用 ----------
CREATE TABLE IF NOT EXISTS memory_atoms (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_memory_id INTEGER NOT NULL,
    atom_type TEXT NOT NULL DEFAULT 'unknown',
    content TEXT NOT NULL,
    entities TEXT DEFAULT '[]',
    importance REAL NOT NULL DEFAULT 0.5,
    confidence REAL NOT NULL DEFAULT 0.7,
    created_at REAL NOT NULL,
    last_accessed_at REAL NOT NULL,
    last_reinforced_at REAL,
    event_time REAL,
    ttl_days REAL NOT NULL DEFAULT 30.0,
    expires_at REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    reinforcement_count INTEGER NOT NULL DEFAULT 0,
    decay_type TEXT NOT NULL DEFAULT 'exponential',
    session_id TEXT,
    persona_id TEXT,
    metadata TEXT DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_atoms_parent ON memory_atoms(parent_memory_id);
CREATE INDEX IF NOT EXISTS idx_atoms_status ON memory_atoms(status);
CREATE INDEX IF NOT EXISTS idx_atoms_expires ON memory_atoms(expires_at);
CREATE INDEX IF NOT EXISTS idx_atoms_session ON memory_atoms(session_id);
CREATE INDEX IF NOT EXISTS idx_atoms_persona ON memory_atoms(persona_id);
CREATE INDEX IF NOT EXISTS idx_atoms_status_expires ON memory_atoms(status, expires_at);

-- ---------- memory_vectors：向量召回用的 float32 BLOB ----------
CREATE TABLE IF NOT EXISTS memory_vectors (
    doc_id TEXT PRIMARY KEY,
    vec BLOB NOT NULL,
    ts REAL NOT NULL
);
