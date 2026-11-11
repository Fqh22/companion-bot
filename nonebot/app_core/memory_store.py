"""memory 的 SQLite 存储与维护操作。

方法通过 self._conn() 拿连接，所以 Memory.DB 的测试补丁仍然有效。
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import sqlite3
import time
import uuid

import numpy as np

from app_core import config
from app_core.memory_common import PERSONA_ID, SHADOW_DEDUP_COS
from app_core.vec import cos as _cos
from app_core.vec import pack as _pack
from app_core.vec import unpack as _unpack

log = logging.getLogger("qqbot.memory")


class StoreMixin:
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

    def delete(self, doc_id: str) -> bool:
        """真删一条记忆（用户要求忘记）。不可恢复；内部清理请走归档。

        逐表 DELETE 各自 try：老库里 memory_ranges 的列可能不同，
        删不掉不该让整条记忆删一半还报错。
        """
        c = self._conn()
        try:
            row = c.execute("SELECT id FROM documents WHERE doc_id=?", (doc_id,)).fetchone()
            if not row:
                return False
            mid = int(row[0])
            with c:
                for sql, args in (
                    ("DELETE FROM memory_atoms WHERE parent_memory_id=?", (mid,)),
                    ("DELETE FROM memory_vectors WHERE doc_id=?", (doc_id,)),
                    ("DELETE FROM memory_ranges WHERE doc_id=?", (doc_id,)),
                    ("DELETE FROM documents WHERE doc_id=?", (doc_id,)),
                ):
                    try:
                        c.execute(sql, args)
                    except Exception:  # noqa: BLE001
                        pass
            return True
        finally:
            c.close()

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
