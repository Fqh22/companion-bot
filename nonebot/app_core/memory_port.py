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

from dataclasses import dataclass, field
from typing import Protocol

# 契约只声明**已实现**的类型。
# 声明了却返回空列表，调用方会以为「没有相关记忆」——这和 2026-11-05 的
# meme.find() 吞异常是同一类毛病：缺口无声。未实现的类型必须显式报错。
MEMORY_TYPES = ("fact", "profile", "knowledge")
UNIMPLEMENTED_TYPES = ("mood", "task")


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
                     touch: bool = True) -> list[MemoryItem]: ...

    async def remember(self, item: MemoryItem) -> str: ...

    async def forget(self, ref: str, type: str = "fact") -> bool: ...

    def explain(self) -> list[dict]: ...


class CoreMemoryPort:
    """把现有存储适配成统一契约。

    P1 只实现读取；写入（remember/forget）留给 P2，先抛 NotImplementedError，
    避免"看起来接上了、其实没写"。
    """

    def __init__(self, core) -> None:
        self.core = core
        self._trace: list[dict] = []

    async def recall(self, query: str, types: tuple[str, ...] = MEMORY_TYPES,
                     limit: int = 5, qv: list[float] | None = None,
                     touch: bool = True) -> list[MemoryItem]:
        bad = [t for t in types if t not in MEMORY_TYPES]
        if bad:
            raise NotImplementedError(
                f"记忆类型未实现：{', '.join(bad)}（已实现：{', '.join(MEMORY_TYPES)}）")

        out: list[MemoryItem] = []
        self._trace = []

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
            self._trace.append({"type": "fact", "n": len(rows), "limit": limit})

        if "knowledge" in types:
            try:
                from app_core import wiki

                idx = wiki.index_text()
                if idx:
                    out.append(MemoryItem(type="knowledge", text=idx,
                                          ref="wiki:index", source="wiki"))
                    self._trace.append({"type": "knowledge", "n": 1})
            except Exception:  # noqa: BLE001
                pass

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
                        self._trace.append({"type": "profile", "n": 1})
            except Exception:  # noqa: BLE001
                pass

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
        if item.type != "fact":
            raise NotImplementedError(f"记得的写入类型只有 fact / knowledge，收到 {item.type}")
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
        if type != "fact":
            raise NotImplementedError(f"forget 只支持 fact，收到 {type}")
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

    def explain(self) -> list[dict]:
        """这一轮读了什么：类型、条数、上限。给 /debug trace 用。"""
        return list(self._trace)
