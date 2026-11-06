"""memory 的召回索引：矩阵 / BM25 / 实体图缓存。"""
from __future__ import annotations

import json
import logging
import time

import numpy as np

from app_core import config
from app_core.vec import cos as _cos
from app_core.vec import unpack as _unpack

log = logging.getLogger("qqbot.memory")


class IndexMixin:
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
