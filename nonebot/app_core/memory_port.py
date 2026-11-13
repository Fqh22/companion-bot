"""[orchestration] 记忆契约：入口只通过这里读写记忆。

P0/P1 阶段：**只读适配器**。把现有存储（memory / profile / wiki / kb）映射成统一的
MemoryItem，不改表、不迁数据、不改变行为。

五个类型（见 docs/09）：fact / profile / knowledge / mood / task。
P1 第一刀只迁 fact 召回（最重、最影响行为）；knowledge / profile 仍走旧路径，P3 再迁。

为什么要有这一层：入口（chat_turn / commands / tools / scheduler）现在直接 import
memory / profile / wiki / diary，换存储或加记忆类型要改所有入口。契约定下来后，
入口只认 recall / remember / forget。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Protocol

log = logging.getLogger("qqbot.memory_port")

# 契约只声明**已实现**的类型。
# 声明了却返回空列表，调用方会以为「没有相关记忆」——这和 2026-11-05 的
# meme.find() 吞异常是同一类毛病：缺口无声。未实现的类型必须显式报错。
MEMORY_TYPES = ("fact", "profile", "knowledge", "task")
UNIMPLEMENTED_TYPES = ("mood",)


@dataclass(frozen=True)
class MemoryItem:
    """一条记忆的统一形状；存储可以各异，语义必须一致。"""

    type: str
    text: str
    ref: str = ""
    source: str = ""
    confidence: float = 1.0
    created: float = 0.0
    last_used: float = 0.0
    age_days: float | None = None
    expires: float | None = None
    supersedes: str | None = None
    # 存储相关的附加参数（session / topics / vec / range_key …）。
    # 契约只规定语义字段；具体后端需要的东西放这里，避免契约被某个存储绑架。
    payload: dict = field(default_factory=dict)


class MemoryPort(Protocol):
    async def recall(self, query: str, types: tuple[str, ...] = MEMORY_TYPES,
                     limit: int = 5, qv: list[float] | None = None,
                     touch: bool = True,
                     trace: list[dict] | None = None) -> list[MemoryItem]: ...

    async def remember(self, item: MemoryItem) -> str: ...

    async def forget(self, ref: str, type: str = "fact") -> bool: ...


class CoreMemoryPort:
    """把现有存储适配成统一契约。

    读（recall）与写（remember/forget）都已接上；未实现的类型显式抛错，
    不返回空列表——缺口无声比缺口本身更糟（见 MEMORY_TYPES 的注释）。

    轨迹（trace）**不挂在实例上**：port 是进程内单例（chat.py 的 _memory_port
    缓存到 self._mp），挂在实例上意味着两个并发的轮次会互相覆盖对方的「这一轮
    读了什么」。调用方传自己的 list 进来，各归各的。
    """

    def __init__(self, core) -> None:
        self.core = core

    async def recall(self, query: str, types: tuple[str, ...] = MEMORY_TYPES,
                     limit: int = 5, qv: list[float] | None = None,
                     touch: bool = True,
                     trace: list[dict] | None = None) -> list[MemoryItem]:
        bad = [t for t in types if t not in MEMORY_TYPES]
        if bad:
            raise NotImplementedError(
                f"记忆类型未实现：{', '.join(bad)}（已实现：{', '.join(MEMORY_TYPES)}）")

        out: list[MemoryItem] = []
        # 本地轨迹：只在这次调用内累积，结束时才交给调用方。
        tr: list[dict] = []

        if "fact" in types:
            rows = await self.core.memory.recall(query, limit, qv=qv, touch=touch)
            for r in rows:
                meta = r.get("meta") or {}
                out.append(MemoryItem(
                    type="fact",
                    text=str(r.get("text") or ""),
                    ref=str(r.get("doc_id") or ""),
                    source="memory",
                    created=float(meta.get("create_time") or 0.0),
                    last_used=float(meta.get("last_access_time") or 0.0),
                    age_days=r.get("age_days"),
                    confidence=float(meta.get("importance") or 0.0),
                ))
            tr.append({"type": "fact", "n": len(rows), "limit": limit})

        if "knowledge" in types:
            try:
                from app_core import wiki

                idx = wiki.index_text()
                if idx:
                    out.append(MemoryItem(type="knowledge", text=idx,
                                          ref="wiki:index", source="wiki"))
                    tr.append({"type": "knowledge", "n": 1})
            except Exception:  # noqa: BLE001
                pass

        if "task" in types:
            try:
                from app_core import followup
                from app_core.scheduling import remind

                n = 0
                for r in followup.pending(limit):
                    out.append(MemoryItem(
                        type="task", text=str(r.get("text") or ""),
                        ref=f"followup:{r.get('id')}",
                        source=str(r.get("source") or ""),
                        payload={"kind": "followup", "status": r.get("status"),
                                 "asks": r.get("asks")}))
                    n += 1
                for r in remind.list_pending(limit):
                    out.append(MemoryItem(
                        type="task", text=str(r.get("text") or ""),
                        ref=f"reminder:{r.get('id')}", source="remind",
                        payload={"kind": "reminder",
                                 "due_ts": r.get("due_ts")}))
                    n += 1
                tr.append({"type": "task", "n": n, "limit": limit})
            except Exception as e:  # noqa: BLE001
                log.warning("task 读取失败：%s", e)

        if "profile" in types:
            try:
                from app_core import learn
                from app_core import profile as profile_mod

                fields = learn.profile_fields()
                if fields:
                    text = profile_mod.compact(fields, learn.profile_conf(),
                                               learn.profile_seen())
                    if text:
                        out.append(MemoryItem(type="profile", text=text,
                                              ref="profile", source="review",
                                              confidence=0.7))
                        tr.append({"type": "profile", "n": 1})
            except Exception:  # noqa: BLE001
                pass

        # 轨迹交给调用方自己的 list：并发轮次各记各的，不会互相覆盖。
        if trace is not None:
            trace.extend(tr)
        return out

    async def remember(self, item: MemoryItem) -> str:
        """P2：先只接 fact（总结写入）。其它类型等各自的写入点迁过来再开。"""
        p = item.payload or {}
        if item.type == "knowledge":
            from app_core import learn

            return learn.note_write(
                str(p.get("title") or ""), item.text,
                str(p.get("tag") or ""),
                str(p.get("project") or ""),
                str(p.get("source") or "自撰"),
                int(p.get("stale_days") or 0))
        if item.type == "task":
            kind = str(p.get("kind") or "followup")
            if kind != "followup":
                raise NotImplementedError(
                    f"task 创建只接 followup，收到 {kind}（reminder 走 remind_me 工具，"
                    "它要解析自然语言时间，不属于契约层）")
            from app_core import followup, followup_store

            if not followup.add(item.text, str(p.get("source") or "memory"),
                                 str(p.get("ref") or "")):
                raise RuntimeError("跟进创建失败（可能近似重复，或不像待办）")
            fid = followup_store.id_for_text(item.text)
            return f"followup:{fid}" if fid else "followup:"

        if item.type != "fact":
            raise NotImplementedError(f"记得的写入类型只有 fact / knowledge / task，收到 {item.type}")
        return self.core.memory.add_document(
            text=item.text,
            session=str(p.get("session") or ""),
            importance=float(item.confidence),
            topics=list(p.get("topics") or []),
            key_facts=list(p.get("key_facts") or []),
            vec=p.get("vec"),
            followups=list(p.get("followups") or []),
            range_key=str(p.get("range_key") or ""),
        )

    async def forget(self, ref: str, type: str = "fact") -> bool:
        """真删一条 fact（ref = doc_id）。用户要求忘记时走这里，不走归档。

        type 只支持 fact；传其它类型显式报错，而不是返回 False——
        "没找到"和"这类型不支持"必须能区分。
        """
        if type == "task":
            kind, _, sid = ref.partition(":")
            try:
                rid = int(sid)
            except ValueError:
                return False
            if kind == "followup":
                from app_core import followup_store

                followup_store.set_status(rid, "cancelled")
                return True
            if kind == "reminder":
                from app_core.scheduling import remind

                return "没有这条提醒" not in remind.cancel(rid)
            raise NotImplementedError(f"未知任务类型：{kind}")

        if type != "fact":
            raise NotImplementedError(f"forget 只支持 fact / task，收到 {type}")
        return bool(self.core.memory.delete(ref))

    async def remember_profile(self, fields: dict, conf: dict | None = None,
                               evidence: dict | None = None,
                               changed: dict | None = None) -> dict:
        """写画像：合并而非替换。返回 update_state 的结果（含 dropped）。"""
        from app_core import learn

        return learn.save_profile(fields or {}, conf or {}, evidence or {}, changed or {})

    async def resolve(self, prefix: str) -> list[str]:
        """按 doc_id 前缀找完整 id（/forget id:xxxx 用）。"""
        return list(self.core.memory.find_by_prefix(prefix))
