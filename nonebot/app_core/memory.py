"""记忆层：复用 LivingMemory 的 schema（documents / memory_atoms），
自己管一张向量表。不依赖 AstrBot。
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import sqlite3
import time
import uuid
from pathlib import Path

from app_core import paths as _paths

import httpx

from app_core import config
from app_core import memory_score
from app_core.vec import cos as _cos
from app_core.vec import pack as _pack
from app_core.vec import unpack as _unpack

DB = _paths.MEM_DB
KEYS = _paths.KEYS

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






from app_core.memory_embed import EmbedMixin
from app_core.memory_index import IndexMixin
from app_core.memory_store import StoreMixin


class Memory(StoreMixin, IndexMixin, EmbedMixin):
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
        # 2026-11-14：影子实验判据被证伪（333 次/2.97 低于转正阈值但人工抽检否决），
        # 按「到期即关」关闭。要重开请先重写判据，别再拿重合度当代理指标。
        self.shadow_recall = bool(mem_cfg.get("shadow_retrieval", False))
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

    def init(self) -> None:
        c = self._conn()
        # 全新机器上这个库是空的：先把完整表结构建出来。
        # 云端一直是老库（AstrBot 时代建好的），所以之前只补了 memory_vectors 等三张表，
        # documents / memory_atoms 靠历史安装遗留——新装的人会直接崩在第一条记忆上。
        try:
            schema = Path(__file__).with_name("memory_schema.sql")
            if schema.exists():
                c.executescript(schema.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            log.warning("记忆库建表失败（schema）：%s", e)
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

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(DB, timeout=15)
        c.execute("PRAGMA journal_mode=WAL")
        return c

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

        # 时间 + 重要性 + 重复召回降权：纯函数在 memory_score，改排序不用读整个 Memory。
        final = memory_score.rank(
            scores, self._imp, self._ctime, self._last, time.time(),
            self.time_tau_days, self.time_w, self.imp_w, self.keep_important,
            self.repeat_window_min, self.repeat_penalty)

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
