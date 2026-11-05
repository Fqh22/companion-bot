"""记忆层：复用 LivingMemory 的 schema（documents / memory_atoms），
自己管一张向量表。不依赖 AstrBot。
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import sqlite3
import struct
import time
import uuid
from pathlib import Path

import httpx

from app_core import config

DB = Path("/opt/qqbot-data/livingmemory.db")
KEYS = Path("/opt/qqbot-data/keys.json")

log = logging.getLogger("qqbot.memory")

EMBED_API = "https://open.bigmodel.cn/api/paas/v4/embeddings"
EMBED_MODEL = "embedding-3"
PERSONA_ID = "editor-assistant"
# 影子去重阈值：新记忆与已有活跃记忆的 cos 达到这个值就认为「说的是同一件事」。
# 现在**只记日志、不合并**——先攒一周看误判率，再决定要不要真的改成更新旧记忆。
SHADOW_DEDUP_COS = 0.85


def _zhipu_key() -> str:
    try:
        return json.loads(KEYS.read_text(encoding="utf-8")).get("zhipu", "")
    except Exception:
        return ""


# embedding 结果缓存：同一条文本不重复调外部接口。
# 键是文本的 sha1，值就是向量；条数很少（同一个 query 一轮只算一次），
# 主要是省掉「同一句重复触发召回」和「ensure_vectors 重跑」的浪费。
_EMBED_CACHE: dict[str, list[float]] = {}
EMBED_CACHE_MAX = 256


def _pack(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def _unpack(blob: bytes) -> list[float]:
    n = len(blob) // 4
    return list(struct.unpack(f"{n}f", blob))


def _cos(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


class Memory:
    def __init__(self) -> None:
        self.key = _zhipu_key()
        cfg = config.load()["embedding"]
        self.api = cfg.get("api") or EMBED_API
        self.model = cfg.get("model") or EMBED_MODEL
        mem_cfg = config.load().get("memory") or {}
        self.recall_min_score = float(mem_cfg.get("recall_min_score", 0.35))
        # 时间 / 重要性调制参数（见 recall 里的公式）
        self.time_tau_days = max(1.0, float(mem_cfg.get("recall_time_tau_days", 30)))
        # 「刚说过的事」降权：窗口内被召回过的记忆乘 (1 - repeat_penalty×剩余比例)
        self.repeat_window_min = max(1.0, float(mem_cfg.get("recall_repeat_window_min", 30)))
        self.repeat_penalty = max(0.0, min(0.9, float(mem_cfg.get("recall_repeat_penalty", 0.35))))
        self.time_w = float(mem_cfg.get("recall_time_weight", 0.3))
        self.imp_w = float(mem_cfg.get("recall_importance_weight", 0.1))
        self.keep_important = float(mem_cfg.get("recall_keep_important", 0.8))
        # 关键词通道（BM25+RRF）目前只跑影子：算完 dense 结果再算一遍，
        # 落盘对比，**不改召回结果**。攒够样本再决定要不要真的切过去。
        self.shadow_recall = bool(mem_cfg.get("shadow_retrieval", True))
        self._bm25 = None
        self._bm25_ids: list[str] = []
        self._bm25_n = -1
        # 实体连边（共现实体），同样只跑影子
        self._graph = None
        self._graph_n = -1
        # 主题词表（给摘要复用），带 TTL 缓存
        self._vocab: list[str] | None = None
        self._vocab_ts = 0.0
        # 归一化向量矩阵的进程内缓存（条数一变就重建）
        self._mat = None
        self._ids: list[str] = []
        self._mat_n = -1
        self._imp = None
        self._last = None
        self._ctime = None

    # ---------- 基础 ----------
    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(DB, timeout=15)
        c.execute("PRAGMA journal_mode=WAL")
        return c

    def init(self) -> None:
        c = self._conn()
        with c:
            c.execute(
                "CREATE TABLE IF NOT EXISTS memory_vectors ("
                "doc_id TEXT PRIMARY KEY, vec BLOB NOT NULL, ts REAL NOT NULL)"
            )
            # 幂等键：一段对话只固化一次。崩溃在「写完记忆、还没推水位」之间时，
            # 重跑会撞上这个主键，直接返回已有 doc_id。
            c.execute(
                "CREATE TABLE IF NOT EXISTS memory_ranges ("
                "range_key TEXT PRIMARY KEY, doc_id TEXT NOT NULL, ts REAL NOT NULL)"
            )
            # 记忆被重写前留一份原样，回滚就是一条 INSERT ... SELECT。
            # 只由 ops/rebuild.py 写，正常运行不会碰它。
            c.execute(
                "CREATE TABLE IF NOT EXISTS documents_history ("
                "doc_id TEXT NOT NULL, text TEXT, metadata TEXT, atoms TEXT, "
                "replaced_at REAL NOT NULL)"
            )
        c.close()
        # 原子事实去重：同一句话不再重复插入 memory_atoms
        try:
            c = self._conn()
            with c:
                try:
                    c.execute("ALTER TABLE memory_atoms ADD COLUMN content_hash TEXT")
                except Exception:
                    pass
                c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_atom_hash "
                          "ON memory_atoms(content_hash)")
            c.close()
        except Exception:
            pass
        try:
            # 预热：numpy 首次 import 要 200-300ms，放在启动时比放在
            # 用户第一条消息上更划算；顺带把矩阵先建好。
            self._matrix()
        except Exception:
            pass

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        out: list[list[float] | None] = [None] * len(texts)
        todo: list[tuple[int, str, str]] = []
        for i, t in enumerate(texts):
            if not (t or "").strip():
                # 空串会让智谱直接 400（code 1210），一个空块就能毒掉整批请求
                out[i] = []
                continue
            key = hashlib.sha1((t or "").encode("utf-8")).hexdigest()
            hit = _EMBED_CACHE.get(key)
            if hit is not None:
                out[i] = hit
            else:
                todo.append((i, key, t))
        if todo:
            async with httpx.AsyncClient(timeout=60) as client:
                r = await client.post(
                    self.api,
                    headers={"Authorization": f"Bearer {self.key}"},
                    json={"model": self.model, "input": [t for _, _, t in todo]},
                )
                r.raise_for_status()
                data = r.json().get("data", [])
            for item in data:
                pos = item.get("index", 0)
                if 0 <= pos < len(todo):
                    i, key, _ = todo[pos]
                    out[i] = item.get("embedding")
                    if len(_EMBED_CACHE) >= EMBED_CACHE_MAX:
                        _EMBED_CACHE.clear()
                    _EMBED_CACHE[key] = out[i]
        return [v or [] for v in out]

    # ---------- 写入 ----------
    def _shadow_dedup(self, text: str, vec: list[float] | None) -> None:
        """影子模式去重：只记录「本该合并」，不真的合并。

        同一个事实会在不同日期的总结里反复出现（「他在准备面试」可能散在 5 条记忆里），
        召回时 top-5 里挤进来 3 条重复内容，等于白白挤掉别的东西。

        为什么不直接合并：合并会改写已有记忆、改变召回形态，误合并比重复更难发现
        （一次 0.86 的误判就把两条不相干的事粘成一条，且不可逆）。所以先只打日志，
        攒一周看 `[记忆去重]` 命中的是不是真的重复，再决定开不开。
        """
        if not vec:
            return
        try:
            import numpy as np

            mat, ids = self._matrix()
            if mat is None:
                return
            q = np.asarray(vec, dtype=np.float32)
            if q.shape[0] != mat.shape[1]:
                return
            q = q / (np.linalg.norm(q) or 1.0)
            scores = mat @ q
            i = int(scores.argmax())
            top = float(scores[i])
            if top < SHADOW_DEDUP_COS:
                return
            hit = self._docs_by_ids([ids[i]]).get(ids[i])
            if not hit:            # 已归档的不算重复：它本来就不参与召回
                return
            log.info("[记忆去重] 与 %s cos=%.3f（影子模式：本次仍新建）新=%s 旧=%s",
                     ids[i][:8], top, (text or "")[:40], (hit.get("text") or "")[:40])
        except Exception as e:  # noqa: BLE001
            log.debug("影子去重检查失败（不影响写入）：%s", e)

    def _dup_window(self) -> float:
        try:
            cfg = config.load().get("memory") or {}
            return max(0.0, float(cfg.get("dup_window_minutes", 10)) * 60.0)
        except Exception:  # noqa: BLE001
            return 600.0

    def _recent_dup(self, text: str) -> str:
        """窗口内有没有完全相同的记忆。返回已有 doc_id 或空串。

        只比「归一化后完全相同」，不做相似度：相似去重已经交给复盘和矛盾检测了，
        这里要挡的是「同一段对话被固化两次」这种硬重复。
        """
        window = self._dup_window()
        if window <= 0:
            return ""
        t = " ".join(str(text or "").split())
        if not t:
            return ""
        cutoff = time.time() - window
        c = self._conn()
        try:
            rows = c.execute(
                "SELECT doc_id, text, metadata FROM documents "
                "ORDER BY id DESC LIMIT 200").fetchall()
        finally:
            c.close()
        for doc_id, old_text, meta in rows:
            try:
                m = json.loads(meta or "{}")
            except Exception:  # noqa: BLE001
                m = {}
            if float(m.get("create_time") or 0) < cutoff:
                continue
            if " ".join(str(old_text or "").split()) == t:
                return str(doc_id)
        return ""

    def add_document(
        self,
        text: str,
        session: str,
        importance: float,
        topics: list[str],
        key_facts: list[str],
        atoms: list[str] | None = None,
        vec: list[float] | None = None,
        followups: list[str] | None = None,
        range_key: str = "",
    ) -> str:
        # 幂等：同一段对话重复固化时直接返回已有 doc_id，不写第二条
        if range_key:
            c0 = self._conn()
            hit = c0.execute(
                "SELECT doc_id FROM memory_ranges WHERE range_key=?", (range_key,)).fetchone()
            c0.close()
            if hit:
                log.info("记忆固化幂等命中，跳过重复写入：%s", range_key)
                return hit[0]
        # 第二道网：同一段文字（去空白后完全相同）在窗口内不重复写。
        # range_key 只保护「带 range 的新记忆」——线上有 24 篇是加 range 之前的老数据，
        # 那批只能靠文本去重。窗口默认 10 分钟（memory.dup_window_minutes）。
        dup = self._recent_dup(text)
        if dup:
            log.info("窗口内已有同样内容，跳过重复写入：%s", dup[:8])
            return dup
        self._shadow_dedup(text, vec)
        doc_id = str(uuid.uuid4())
        now = time.time()
        meta = {
            "session_id": session,
            "persona_id": PERSONA_ID,
            "importance": importance,
            "create_time": now,
            "last_access_time": now,
            "status": "active",
            "topics": topics,
            "key_facts": key_facts,
            # 他说过要做但还没做完的事，主动消息优先从这里挑话题
            "followups": followups or [],
            # 来源可追溯：哪段对话、哪个水位区间产生的
            "source_range": range_key,
        }
        c = self._conn()
        with c:
            cur = c.execute(
                "INSERT INTO documents(doc_id, text, metadata, created_at, updated_at) "
                "VALUES (?,?,?,?,?)",
                (doc_id, text, json.dumps(meta, ensure_ascii=False),
                 time.strftime("%Y-%m-%d %H:%M:%S"), time.strftime("%Y-%m-%d %H:%M:%S")),
            )
            doc_row = cur.lastrowid
            if range_key:
                c.execute(
                    "INSERT OR IGNORE INTO memory_ranges(range_key, doc_id, ts) "
                    "VALUES (?,?,?)", (range_key, doc_id, now))
            for fact in (atoms or key_facts):
                # ttl_days / expires_at 是 livingmemory 遗留的表字段（NOT NULL），
                # 当前清理逻辑并不读它们——真正生效的是 documents.metadata.status
                # 加 cleanup() 的时间阈值。别指望这里的 ttl 会自动让原子过期。
                ttl = 30.0 if importance >= 0.7 else 7.0
                # content_hash 上有唯一索引：同一句事实只留一条
                atom_hash = hashlib.sha1(
                    f"{fact}".strip().encode("utf-8")).hexdigest()
                c.execute(
                    "INSERT OR IGNORE INTO memory_atoms(parent_memory_id, atom_type, content, "
                    "entities, importance, confidence, created_at, last_accessed_at, "
                    "last_reinforced_at, ttl_days, expires_at, status, reinforcement_count, "
                    "decay_type, session_id, persona_id, metadata, content_hash) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (doc_row, "fact", fact, json.dumps(topics, ensure_ascii=False),
                     importance, 0.85, now, now, now, ttl, now + ttl * 86400,
                     "active", 0, "step", session, PERSONA_ID,
                     json.dumps({"doc_id": doc_id, "range": range_key},
                                ensure_ascii=False), atom_hash),
                )
            if vec:
                c.execute(
                    "INSERT OR REPLACE INTO memory_vectors(doc_id, vec, ts) VALUES (?,?,?)",
                    (doc_id, _pack(vec), now),
                )
        c.close()
        self._mat = None          # 向量变了，下次召回重建矩阵
        self._bm25_n = -1         # 同一 doc_id 覆盖写不会改变条数，关键词索引也得作废
        return doc_id

    # ---------- 召回 ----------
    def _matrix(self):
        """把全部向量读成**归一化后的矩阵**，进程内缓存。

        归一化是为了让点积直接等于余弦：省掉逐条算模长。
        条数变了（新记忆写入 / ensure_vectors 补向量）就重建。

        为什么不用向量库：2 万条也才 156 MB、7.6 ms 全扫描。
        FAISS/Chroma 的价值在十万级以上 + 高并发 + 元数据联合过滤，
        单用户机器人用不上，反而多一层运维。
        """
        import numpy as np

        c = self._conn()
        n = c.execute("SELECT COUNT(*) FROM memory_vectors").fetchone()[0]
        if self._mat is not None and self._mat_n == n:
            c.close()
            return self._mat, self._ids
        rows = c.execute(
            "SELECT v.doc_id, v.vec, d.metadata "
            "FROM memory_vectors v LEFT JOIN documents d ON d.doc_id = v.doc_id"
        ).fetchall()
        c.close()
        if not rows:
            self._mat, self._ids, self._mat_n = None, [], 0
            self._imp = self._ctime = None
            return None, []
        # 维度必须一致才能拼矩阵。换过 embedding 模型、或者某个流程塞了错维度的
        # 向量时，这里会直接 reshape 爆炸；取「多数派维度」，把异类跳过并记一笔。
        lens = [len(r[1]) // 4 for r in rows]
        dim = max(set(lens), key=lens.count)
        if any(x != dim for x in lens):
            bad = sum(1 for x in lens if x != dim)
            log.warning("跳过 %d 条维度不一致的记忆向量（保留 %d 维）", bad, dim)
            rows = [r for r, x in zip(rows, lens) if x == dim]
            if not rows:
                self._mat, self._ids, self._mat_n = None, [], 0
                self._imp = self._ctime = None
                return None, []
        ids = [r[0] for r in rows]
        flat = np.frombuffer(b"".join(r[1] for r in rows), dtype=np.float32)
        mat = flat.reshape(len(rows), dim)
        norms = np.linalg.norm(mat, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        self._mat = mat / norms
        self._ids = ids
        self._mat_n = n
        # 时间与重要性，用于打分调制
        imp = np.empty(len(rows), dtype=np.float32)
        ctime = np.empty(len(rows), dtype=np.float32)
        last = np.zeros(len(rows), dtype=np.float32)
        for i, r in enumerate(rows):
            try:
                m = json.loads(r[2] or "{}")
            except Exception:
                m = {}
            imp[i] = float(m.get("importance", 0.5))
            ctime[i] = float(m.get("create_time") or 0)
            last[i] = float(m.get("last_access_time") or 0)
        self._imp = imp
        self._ctime = ctime
        self._last = last
        return self._mat, self._ids

    def _all_vectors(self) -> list[tuple[str, list[float]]]:
        """兼容旧调用：返回未归一化的 (doc_id, list)。召回已不再用它。"""
        c = self._conn()
        rows = c.execute("SELECT doc_id, vec FROM memory_vectors").fetchall()
        c.close()
        return [(d, _unpack(v)) for d, v in rows]

    def _docs_by_ids(self, ids: list[str]) -> dict[str, dict]:
        if not ids:
            return {}
        c = self._conn()
        q = ",".join("?" * len(ids))
        rows = c.execute(
            f"SELECT doc_id, text, metadata FROM documents WHERE doc_id IN ({q})", ids
        ).fetchall()
        c.close()
        out = {}
        for doc_id, text, meta in rows:
            try:
                m = json.loads(meta or "{}")
            except Exception:
                m = {}
            if m.get("status", "active") != "active":
                continue
            out[doc_id] = {"doc_id": doc_id, "text": text, "meta": m}
        return out

    def recent(self, n: int = 3, touch: bool = True) -> list[dict]:
        c = self._conn()
        rows = c.execute(
            "SELECT doc_id, text, metadata FROM documents ORDER BY id DESC LIMIT ?", (n,)
        ).fetchall()
        c.close()
        out = []
        for doc_id, text, meta in rows:
            try:
                m = json.loads(meta or "{}")
            except Exception:
                m = {}
            if m.get("status", "active") != "active":
                continue
            out.append({"doc_id": doc_id, "text": text, "meta": m})
        for d in out:
            ct = float((d.get("meta") or {}).get("create_time") or 0)
            d["age_days"] = round((time.time() - ct) / 86400.0, 1) if ct else None
        if touch:
            self.touch([d["doc_id"] for d in out])
        return out

    def touch(self, doc_ids: list[str]) -> None:
        """标记这些记忆「被想起来过」。

        没有这一步，`last_access_time` 永远停在创建时刻，cleanup() 会在 15 天后
        把每一条记忆都当成「长期没被想起」归档掉——哪怕它天天被召回。
        """
        if not doc_ids:
            return
        now = time.time()
        c = self._conn()
        try:
            q = ",".join("?" * len(doc_ids))
            rows = c.execute(
                f"SELECT id, metadata FROM documents WHERE doc_id IN ({q})", doc_ids
            ).fetchall()
            with c:
                for doc_row, meta in rows:
                    try:
                        m = json.loads(meta or "{}")
                    except Exception:
                        m = {}
                    m["last_access_time"] = now
                    m["access_count"] = int(m.get("access_count") or 0) + 1
                    c.execute("UPDATE documents SET metadata=? WHERE id=?",
                              (json.dumps(m, ensure_ascii=False), doc_row))
                    c.execute(
                        "UPDATE memory_atoms SET last_accessed_at=? "
                        "WHERE parent_memory_id=? AND status='active'", (now, doc_row))
        finally:
            c.close()
        # 进程内的 _last 也要跟着更新：它只在矩阵重建时重读，
        # 不更新的话「30 分钟内重复召回降权」要等下一次重建（约 8 轮）才生效，
        # 期间同一条记忆会连续霸占 top-5，对话一直绕回同一个梗。
        try:
            if self._last is not None and self._ids:
                pos = {d: i for i, d in enumerate(self._ids)}
                for doc_id in doc_ids:
                    i = pos.get(doc_id)
                    if i is not None and i < len(self._last):
                        self._last[i] = now
        except Exception:  # noqa: BLE001
            pass

    def forgotten_important(self, limit: int = 3, min_importance: float = 0.5,
                            min_idle_days: float = 7) -> list[dict]:
        """找「重要、但很久没被想起」的记忆。

        注意方向和 recall 相反：召回要新的，主动消息要旧的——
        「突然想起你之前说过的事」才有意义，找昨天刚聊的等于复读。
        """
        now = time.time()
        c = self._conn()
        rows = c.execute("SELECT doc_id, text, metadata FROM documents").fetchall()
        c.close()
        out = []
        for doc_id, text, meta in rows:
            try:
                m = json.loads(meta or "{}")
            except Exception:
                continue
            if m.get("status", "active") != "active":
                continue
            imp = float(m.get("importance", 0.5))
            if imp < min_importance:
                continue
            last = float(m.get("last_access_time") or m.get("create_time") or 0)
            idle = (now - last) / 86400.0 if last else 0.0
            if idle < min_idle_days:
                continue
            # 越重要、越久没被想起越靠前；超过 90 天的按 90 算，避免陈年老账霸榜
            score = imp * min(idle, 90.0) / 30.0
            out.append((score, doc_id, text))
        out.sort(key=lambda x: -x[0])
        return [{"doc_id": d, "text": t} for _, d, t in out[:limit]]

    async def read_full(self, query: str, qv: list[float] | None = None) -> str:
        """按 query 找最相关的一条记忆，返回全文（注入时只给 220 字）。"""
        rows = await self.recall(query, 1, qv=qv)
        if not rows:
            return "没找到相关记忆"
        r = rows[0]
        age = r.get("age_days")
        head = f"（{age:.0f} 天前的记录）\n" if age else ""
        return head + (r.get("text") or "")[:1500]

    async def recall(self, query: str, top_k: int = 5,
                     qv: list[float] | None = None,
                     touch: bool = True) -> list[dict]:
        """按向量召回，返回前会刷新被召回记忆的访问时间。

        qv 可由调用方预先算好传入，避免同一次回复里重复 embedding。
        touch=False 给影子用：只想知道"会捞到什么"，不该改变"被想起过"的记账，
        否则影子里那一次会把访问时间推高，污染清理和重复召回降权。
        """
        mat, ids = self._matrix()
        if mat is None:
            return self.recent(top_k, touch=touch)
        if qv is None:
            qv = (await self.embed([query]))[0]
        if not qv:
            return self.recent(top_k, touch=touch)

        import numpy as np

        q = np.asarray(qv, dtype=np.float32)
        nq = float(np.linalg.norm(q))
        if nq == 0:
            return self.recent(top_k, touch=touch)
        if mat.shape[1] != q.shape[0]:
            # 维度不一致：换过 embedding 模型，或库里混着旧维度的向量。
            # 这种情况矩阵乘会直接抛异常，退回到最近几条更稳。
            return self.recent(top_k, touch=touch)
        scores = mat @ (q / nq)          # 矩阵已归一化，点积即余弦

        # 时间 + 重要性调制。相似度仍是主导，时间只在「差不多相关」时起作用，
        # 这样不会让一条很新的无关记忆挤掉一条很旧的强相关记忆。
        #   最终分 = cos × (1 - w + w×衰减) × (1 + w_i×重要性)
        #   衰减 = exp(-距今天数 / τ)，importance ≥ keep_important 时不做衰减（保底）
        if self._imp is not None and self._ctime is not None:
            age_days = np.maximum(0.0, (time.time() - self._ctime) / 86400.0)
            rec = np.exp(-age_days / self.time_tau_days)
            rec = np.where(self._imp >= self.keep_important, 1.0, rec)
            final = scores * ((1.0 - self.time_w + self.time_w * rec)
                              * (1.0 + self.imp_w * self._imp))
        else:
            final = scores

        # 「刚说过的事」降权：一条记忆 30 分钟内被反复注入，会让对话一直绕回同一个梗。
        # 实测一条记忆被召回 184 次（占全部召回的 6%），就是这个问题。
        # 不硬性排除——同一话题连续几轮仍需要它，只是别每轮都排第一。
        if self._last is not None and len(self._last) == final.shape[0]:
            idle_min = (time.time() - self._last) / 60.0
            fresh = np.clip((self.repeat_window_min - idle_min) / self.repeat_window_min, 0, 1)
            final = final * (1.0 - self.repeat_penalty * fresh)

        k = min(top_k, len(ids))
        # 候选放宽到 3k：已归档的记忆会在 _docs_by_ids 里被过滤掉，
        # 只取 k 个的话，清理跑得越久、召回能凑齐的条数越少
        # （表现是「它明明记得，却说没找到相关的」）。
        order = np.argsort(-final)[:min(len(ids), max(k * 3, k))]
        # 0.35 是实测出来的切点：相关 query 的 top-5 均在 0.36–0.48，
        # 无关 query（如"我喜欢吃什么"而库里没有饮食记忆）整体落在 0.24–0.28。
        # 原来的 0.25 几乎总是凑满 5 条，等于每轮都硬塞 1100 字无关记忆。
        top = [ids[i] for i in order if scores[i] > self.recall_min_score]
        docs = self._docs_by_ids(top)
        out = [docs[d] for d in top if d in docs][:k]
        # 日志/影子只认真正进 prompt 的那几条
        _keep = {d["doc_id"] for d in out}
        order = np.asarray([i for i in order if ids[i] in _keep])
        # 带上「多久以前」：没有时间戳的话，「下周三出差」和「下周四出差」
        # 会并列出现，模型没法判断哪条更新。
        for d in out:
            ct = float((d.get("meta") or {}).get("create_time") or 0)
            d["age_days"] = round((time.time() - ct) / 86400.0, 1) if ct else None
        if touch:
            self.touch([d["doc_id"] for d in out])
        # 调参用：看时间/重要性调制到底改变了什么。
        # 日志里 cos 是原始相似度，f 是调制后的分数，两者排序不同就说明时间起作用了。
        if order.size:
            try:
                parts = []
                for i in order[:3]:
                    age = (time.time() - float(self._ctime[i])) / 86400.0 if self._ctime is not None else 0
                    imp = float(self._imp[i]) if self._imp is not None else 0
                    parts.append(f"{ids[i][:6]} cos={scores[i]:.3f} f={final[i]:.3f} "
                                 f"age={age:.0f}d imp={imp:.2f}")
                log.info("召回 %r → %s", (query or "")[:14], " | ".join(parts))
            except Exception:
                pass
        if self.shadow_recall:
            try:
                self._shadow_retrieval(
                    query, [ids[i] for i in order], [d["doc_id"] for d in out], k)
                self._shadow_graph(query, [d["doc_id"] for d in out])
            except Exception as e:  # noqa: BLE001
                log.debug("召回影子对比失败：%s", e)
        return out

    # ---------- 关键词通道（影子） ----------
    def _bm25_index(self):
        """建／取关键词索引，与 `_matrix` 同频失效（库条数一变就重建）。

        索引顺序是 `memory_vectors JOIN documents` 的自然顺序，跟 dense 的
        `_ids` 不一定一一对应（维度不一致的向量会被 `_matrix` 跳过），
        所以两边只按 doc_id 对齐，绝不按下标。
        """
        from app_core import retrieval

        c = self._conn()
        n = c.execute("SELECT COUNT(*) FROM memory_vectors").fetchone()[0]
        if self._bm25 is not None and self._bm25_n == n:
            c.close()
            return self._bm25, self._bm25_ids
        rows = c.execute(
            "SELECT v.doc_id, d.text FROM memory_vectors v "
            "LEFT JOIN documents d ON d.doc_id = v.doc_id"
        ).fetchall()
        c.close()
        if not rows:
            self._bm25, self._bm25_ids, self._bm25_n = None, [], 0
            return None, []
        self._bm25_ids = [r[0] for r in rows]
        self._bm25 = retrieval.BM25([r[1] or "" for r in rows])
        self._bm25_n = n
        return self._bm25, self._bm25_ids

    def _shadow_retrieval(self, query: str, dense_ids: list[str],
                          top_ids: list[str], k: int) -> None:
        """算一遍 BM25+RRF，跟 dense 的 top-k 比一比，只落盘。

        真要切过去之前必须有数据回答两个问题：提升的是哪类 query、
        有没有把原本对的挤掉。所以记的是两路各自的名单，不是只记个差异数。
        """
        from app_core import retrieval, shadow

        bm25, b_ids = self._bm25_index()
        if bm25 is None or not b_ids:
            return
        b_rank = [b_ids[i] for i in bm25.ranked(query, limit=20)]
        fused = retrieval.fuse(dense_ids[:20], b_rank, limit=k)
        shadow.record_retrieval(query, dense=top_ids, bm25=b_rank[:k], fused=fused)

    # ---------- 主题词表 ----------
    def topic_vocab(self, limit: int = 50, ttl: float = 600.0) -> list[str]:
        """已有主题词的高频表，给摘要 prompt 用（复用而不是再造新词）。

        扫一遍 documents.metadata 是几百 KB 的事，缓存十分钟足够——词表晚生效
        十分钟最多让模型少复用几个标签，不影响正确性。
        """
        now = time.time()
        if self._vocab is not None and now - self._vocab_ts < ttl:
            return self._vocab
        c = self._conn()
        rows = c.execute("SELECT metadata FROM documents").fetchall()
        c.close()
        lists: list[list[str]] = []
        for (meta,) in rows:
            try:
                m = json.loads(meta or "{}")
            except Exception:
                continue
            if isinstance(m.get("topics"), list):
                lists.append(m["topics"])
        from app_core import vocab as vocab_mod

        self._vocab = vocab_mod.build(lists, limit)
        self._vocab_ts = now
        return self._vocab

    # ---------- 实体连边（影子） ----------
    def _graph_index(self):
        """建／取实体图，原子条数一变就重建（新记忆会带新原子）。"""
        from app_core import memgraph

        c = self._conn()
        n = c.execute("SELECT COUNT(*) FROM memory_atoms").fetchone()[0]
        if self._graph is not None and self._graph_n == n:
            c.close()
            return self._graph
        rows = c.execute(
            "SELECT parent_memory_id, entities FROM memory_atoms "
            "WHERE COALESCE(status, 'active') = 'active'"
        ).fetchall()
        c.close()
        parsed = []
        for parent, raw in rows:
            try:
                es = json.loads(raw or "[]")
            except Exception:
                es = []
            if isinstance(es, list):
                parsed.append((parent, es))
        self._graph = memgraph.EntityGraph(parsed)
        self._graph_n = n
        return self._graph

    def _parents_of(self, doc_ids: list[str]) -> list[int]:
        """doc_id（字符串）→ documents.id（整数，连边用的是它）。"""
        if not doc_ids:
            return []
        c = self._conn()
        q = ",".join("?" * len(doc_ids))
        rows = c.execute(
            f"SELECT id FROM documents WHERE doc_id IN ({q})", doc_ids).fetchall()
        c.close()
        return [r[0] for r in rows]

    def _shadow_graph(self, query: str, top_ids: list[str]) -> None:
        """把召回结果的「实体邻居」找出来，落盘。仍然不改返回值。"""
        from app_core import shadow

        graph = self._graph_index()
        if not graph.n:
            return
        seeds = self._parents_of(top_ids)
        rel = graph.related(seeds, limit=5)
        if not rel:
            shadow.record_graph(query, dense=top_ids, expanded=[])
            return
        c = self._conn()
        q = ",".join("?" * len(rel))
        rows = c.execute(
            f"SELECT id, doc_id FROM documents WHERE id IN ({q})", [p for p, _ in rel]
        ).fetchall()
        c.close()
        by_parent = {r[0]: r[1] for r in rows}
        shadow.record_graph(query, dense=top_ids,
                            expanded=[by_parent[p] for p, _ in rel if p in by_parent])

    # ---------- 向量补齐 ----------
    async def ensure_vectors(self, batch: int = 16) -> int:
        c = self._conn()
        have = {r[0] for r in c.execute("SELECT doc_id FROM memory_vectors").fetchall()}
        rows = c.execute("SELECT doc_id, text FROM documents").fetchall()
        c.close()
        todo = [(d, t or "") for d, t in rows if d not in have and (t or "").strip()]
        if not todo:
            return 0
        done = 0
        for i in range(0, len(todo), batch):
            chunk = todo[i:i + batch]
            vecs = await self.embed([t[:2000] for _, t in chunk])
            c = self._conn()
            with c:
                for (doc_id, _), v in zip(chunk, vecs):
                    if v:
                        c.execute(
                            "INSERT OR REPLACE INTO memory_vectors(doc_id, vec, ts) VALUES (?,?,?)",
                            (doc_id, _pack(v), time.time()),
                        )
                        done += 1
            c.close()
        self._mat = None      # 补过向量，缓存作废
        self._bm25_n = -1
        return done

    # ---------- 清理 ----------
    def similar_active(self, vec: list[float], top_k: int = 3,
                       min_score: float = 0.5,
                       exclude: set[str] | None = None) -> list[dict]:
        """找和新向量最像的**活跃**记忆。给矛盾检测用。

        为什么不直接复用 recall：recall 会做时间/重要性调制和重复召回降权，
        回答的是「这一轮该注入哪几条」；矛盾检测问的是「是不是同一件事」，
        要的是原始余弦，不能被「刚被想起来过」压低。
        """
        if not vec:
            return []
        mat, ids = self._matrix()
        if mat is None or not ids:
            return []
        import numpy as np

        q = np.asarray(vec, dtype=np.float32)
        n = float(np.linalg.norm(q))
        if n == 0 or mat.shape[1] != q.shape[0]:
            return []
        scores = mat @ (q / n)
        order = np.argsort(-scores)[:max(top_k * 3, top_k)]
        skip = set(exclude or ())
        picked = [(ids[i], float(scores[i])) for i in order
                  if scores[i] >= min_score and ids[i] not in skip][:max(1, int(top_k))]
        docs = self._docs_by_ids([d for d, _ in picked])
        out = []
        for doc_id, score in picked:
            d = docs.get(doc_id)
            if not d:
                continue          # 已归档/已被取代的，不参与矛盾判断
            out.append({"doc_id": doc_id, "text": d["text"],
                        "meta": d["meta"], "score": score})
        return out

    def supersede(self, old_doc_id: str, by_doc_id: str, why: str = "") -> bool:
        """把一条旧记忆标成「已被取代」。软删：原文、向量、原子都还在。

        和 cleanup 的 archived 是两种不同的「不活跃」：
        archived = 太久没想起，可能是没用了；superseded = 被新事实推翻，明确错了。
        两者都不参与召回，/memory_restore 都能捞回来。
        """
        c = self._conn()
        try:
            row = c.execute("SELECT id, metadata FROM documents WHERE doc_id=?",
                            (old_doc_id,)).fetchone()
            if not row:
                return False
            doc_row, meta = row
            try:
                m = json.loads(meta or "{}")
            except Exception:  # noqa: BLE001
                m = {}
            if m.get("status", "active") != "active":
                return False          # 已经归档/取代过，别覆盖原来的时间线
            m["status"] = "superseded"
            m["superseded_by"] = by_doc_id
            m["superseded_at"] = time.time()
            m["supersede_reason"] = str(why)[:80]
            with c:
                c.execute("UPDATE documents SET metadata=? WHERE id=?",
                          (json.dumps(m, ensure_ascii=False), doc_row))
                c.execute("UPDATE memory_atoms SET status='superseded' "
                          "WHERE parent_memory_id=?", (doc_row,))
            log.info("记忆被取代：%s ← %s（%s）", old_doc_id[:8], by_doc_id[:8], why)
            return True
        finally:
            c.close()

    def superseded(self, limit: int = 10) -> list[dict]:
        """最近的「被取代」记录，给 /memory_conflicts 用。"""
        c = self._conn()
        rows = c.execute("SELECT doc_id, text, metadata FROM documents "
                         "ORDER BY id DESC").fetchall()
        c.close()
        out: list[dict] = []
        for doc_id, text, meta in rows:
            try:
                m = json.loads(meta or "{}")
            except Exception:  # noqa: BLE001
                m = {}
            if m.get("status") != "superseded":
                continue
            out.append({"doc_id": doc_id, "text": text, "meta": m})
            if len(out) >= max(1, int(limit)):
                break
        return out

    def cleanup(self) -> dict:
        """归档「该忘掉」的记忆（软删，`status='archived'`，可恢复）。

        两条规则叠加：

        1) **按重要性分层保留期**（以最后一次被想起为准）
           importance ≥ 0.8 → 180 天
           0.5 ≤ imp < 0.8  → 90 天
           imp < 0.5        → 30 天

           不再用「30 天无差别硬归档」：那会把 importance 0.9 的重要旧事
           一起删掉，等于「越重要的事，放久了越该被忘」，完全反了。

        2) **总量兜底**：活跃记忆超过 max_active 时，按 importance × 新鲜度
           淘汰最差的一批。按每天 5 条算，3000 条约等于一年半的积累；
           到了这个量级召回信噪比才开始下降，所以兜底值定在这。
        """
        cfg = config.load().get("memory") or {}
        days_high = float(cfg.get("cleanup_days_important", 180))
        days_mid = float(cfg.get("cleanup_days_normal", 90))
        days_low = float(cfg.get("cleanup_days_low", 30))
        max_active = int(cfg.get("cleanup_max_active", 3000))
        now = time.time()

        def _archive(c, doc_row: int, m: dict) -> None:
            m["status"] = "archived"
            m["archived_at"] = now
            c.execute("UPDATE documents SET metadata=? WHERE id=?",
                      (json.dumps(m, ensure_ascii=False), doc_row))
            c.execute("UPDATE memory_atoms SET status='archived' WHERE parent_memory_id=?",
                      (doc_row,))

        c = self._conn()
        rows = c.execute("SELECT id, doc_id, text, metadata FROM documents").fetchall()
        archived = 0
        details: list[dict] = []
        alive: list[tuple[int, dict, float, float]] = []
        with c:
            for doc_row, doc_id, text, meta in rows:
                try:
                    m = json.loads(meta or "{}")
                except Exception:
                    m = {}
                if m.get("status", "active") != "active":
                    continue
                imp = float(m.get("importance", 0.5))
                last = float(m.get("last_access_time") or m.get("create_time") or 0)
                keep_days = (days_high if imp >= 0.8
                             else days_mid if imp >= 0.5 else days_low)
                if last and now - last > keep_days * 86400:
                    _archive(c, doc_row, m)
                    archived += 1
                    details.append({"doc_id": doc_id, "importance": round(imp, 2),
                                    "idle_days": int((now - last) / 86400),
                                    "text": (text or "")[:60]})
                else:
                    alive.append((doc_row, doc_id, m, imp, last))

            over = len(alive) - max_active
            if over > 0:
                def value(item):
                    _, _, _, imp, last = item
                    age = max(0.0, now - last) / 86400 if last else 0.0
                    return imp * math.exp(-age / days_mid)

                alive.sort(key=value)
                for doc_row, doc_id, m, _, _ in alive[:over]:
                    _archive(c, doc_row, m)
                    archived += 1
                    details.append({"doc_id": doc_id,
                                    "importance": round(float(m.get("importance", 0.5)), 2),
                                    "idle_days": -1, "text": "（总量兜底淘汰）"})
                alive = alive[over:]
        c.close()
        return {"archived": archived, "alive": len(alive), "details": details}

    def restore(self, qv: list[float], top_k: int = 3,
                min_score: float | None = None) -> list[dict]:
        """从归档池里按语义找回记忆（cleanup 的反向操作）。

        归档只把 `metadata.status` 改成 archived，原文、原子和向量都还在，
        所以能按余弦捞回来。召回本来就过滤 archived，恢复后它立刻重新参与召回，
        不用改别的。

        阈值默认跟召回用同一个 `recall_min_score`：恢复进来的东西如果连召回门槛
        都够不上，等于白恢复。
        """
        if not qv:
            return []
        if min_score is None:
            min_score = float(
                (config.load().get("memory") or {}).get("recall_min_score", 0.35))
        c = self._conn()
        rows = c.execute("SELECT id, doc_id, text, metadata FROM documents").fetchall()
        vecs = dict(c.execute("SELECT doc_id, vec FROM memory_vectors").fetchall())
        cands: list[tuple] = []
        now = time.time()
        for doc_row, doc_id, text, meta in rows:
            try:
                m = json.loads(meta or "{}")
            except Exception:
                m = {}
            if m.get("status") not in ("archived", "superseded"):
                continue
            blob = vecs.get(doc_id)
            if not blob:
                continue
            score = _cos(qv, _unpack(blob))
            if score < min_score:
                continue
            cands.append((score, doc_row, doc_id, text, m))
        cands.sort(key=lambda x: -x[0])
        picked = cands[:max(1, int(top_k))]
        out: list[dict] = []
        with c:
            for score, doc_row, doc_id, text, m in picked:
                m["status"] = "active"
                m["restored_at"] = now
                m["last_access_time"] = now
                c.execute("UPDATE documents SET metadata=? WHERE id=?",
                          (json.dumps(m, ensure_ascii=False), doc_row))
                c.execute(
                    "UPDATE memory_atoms SET status='active' WHERE parent_memory_id=?",
                    (doc_row,))
                out.append({
                    "doc_id": doc_id,
                    "text": text,
                    "score": round(score, 3),
                    "importance": round(float(m.get("importance", 0.5)), 2),
                })
        c.close()
        return out
