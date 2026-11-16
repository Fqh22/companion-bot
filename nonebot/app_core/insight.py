"""[orchestration] 反思：把零散记忆压成「理解」，写回记忆库作为 insight。

为什么需要它：现在的记忆只有「他说过什么」（fact）。模型不会把 294 条事实
想成「他最近焦虑的来源不是找工作本身，是家里催他」——这需要一次主动的归纳。

三条硬规则（不满足就不写）：
1. **必须有 sources**：每条 insight 要指向真实存在的 doc_id，空则丢弃；
2. **不能改写 fact**：insight 只解释，事实冲突时以 fact 为准；
3. **用户否定优先**：被纠正过的 insight 标 superseded，不参与检索。

触发是「每周兜底」；事件触发和纠正链路后续再加。
"""
from __future__ import annotations

import json
import time

from app_core import config

INSIGHT_SYSTEM = (
    "你在整理「对一个人的理解」。给你最近几天的记忆条目（id + 内容），",
    "请归纳出 1-3 条**高层的判断**，而不是复述事实。",
    "要求：",
    "1. 每条判断必须引用至少 2 个来源 id；",
    "2. 判断要能解释「为什么」，不能只是「他做了什么」；",
    "3. 只输出 JSON 数组，每项 {'text': 判断, 'sources': [id, ...]}；",
    "4. 没有把握就不要写——宁可少一条，不要编。",
)
# 上面是多个字面量用逗号分隔 → 元组。content 必须是字符串，
# 否则 payload 里 messages[0].content 变成数组，DeepSeek 直接 422
# （实测：「invalid type: string ..., expected internally tagged enum」）。
INSIGHT_SYSTEM = "".join(INSIGHT_SYSTEM)


def _recent(mem, limit: int = 40) -> list[dict]:
    """最近活跃的记忆（id + 文本），给模型当素材。用传入的实例，不新建。"""
    c = mem._conn()
    # 排除已有的 insight：不能拿「反思」当「反思」的素材，
    # 否则同一套判断会自我强化、越滚越偏。
    rows = c.execute(
        "SELECT doc_id, text FROM documents "
        "WHERE COALESCE(json_extract(metadata, '$.type'), 'fact') != 'insight' "
        "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    c.close()
    return [{"id": r[0], "text": (r[1] or "")[:120]} for r in rows]


def _profile_text() -> str:
    """当前画像，作为「已知」上下文——避免反思重复生成画像里已经有的判断。"""
    try:
        from app_core import paths as _paths
        from app_core import profile as _prof

        fields = _prof.load(_paths.LEARN / "profile.json")
        return "；".join(f"{k}：{v}" for k, v in list(fields.items())[:6])[:500]
    except Exception:  # noqa: BLE001
        return ""


def _rejected(mem, limit: int = 20) -> list[str]:
    """被用户否定过的洞察文本——负面清单，防止同一判断反复生成。"""
    try:
        c = mem._conn()
        rows = c.execute(
            "SELECT text FROM documents WHERE "
            "json_extract(metadata, '$.status')='superseded' AND "
            "COALESCE(json_extract(metadata, '$.type'), 'fact')='insight' "
            "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        c.close()
        return [(r[0] or "") for r in rows]
    except Exception:  # noqa: BLE001
        return []


def _too_similar(text: str, olds: list[str], th: float = 0.55) -> bool:
    """字符二元组 Jaccard。够粗，但能挡住「换个说法重讲同一个判断」。"""
    def grams(s: str) -> set:
        return {s[i:i + 2] for i in range(len(s) - 1)}

    g = grams(text)
    if not g:
        return False
    for o in olds:
        og = grams(o)
        if og and len(g & og) / len(g | og) >= th:
            return True
    return False


async def generate(mem, core, max_items: int = 3) -> list[dict]:
    """跑一次反思并写入。core 提供 complete_raw。返回被写入的 insight 列表。"""
    rows = _recent(mem)
    if len(rows) < 6:
        return []
    lines = ["%s | %s" % (r["id"][:8], r["text"]) for r in rows]
    _prof = _profile_text()
    _sys = INSIGHT_SYSTEM
    if _prof:
        _sys += ("\n\n已知的稳定画像（这些不用再判断，除非有新证据能修正它）：\n" + _prof)
    msgs = [{"role": "system", "content": _sys},
            {"role": "user", "content": "\n".join(lines)[:4000]}]
    try:
        # 用 summary 通道：这是「批处理/总结」性质的任务，和聊天通道的参数不同
        # （chat 通道带 thinking 字段，DeepSeek 对这个任务的 messages 形态更严）。
        raw = await core.complete_summary(msgs, max_tokens=600)
        raw = raw or ""
    except Exception:  # noqa: BLE001
        return []
    start, end = raw.find("["), raw.rfind("]")
    if start < 0 or end <= start:
        return []
    try:
        items = json.loads(raw[start:end + 1])
    except Exception:  # noqa: BLE001
        return []
    # 素材给的是 8 位短 id（省 token），模型会原样返回短 id。
    # 校验要按前缀匹配回完整 doc_id——不然一条都对不上，全部被丢弃（实测踩过）。
    by_prefix = {r["id"][:8]: r["id"] for r in rows}
    out: list[dict] = []
    for it in items[:max_items]:
        if not isinstance(it, dict):
            continue
        text = str(it.get("text") or "").strip()[:200]
        src: list[str] = []
        for s in (it.get("sources") or []):
            full = by_prefix.get(str(s)[:8])
            if full and full not in src:
                src.append(full)
        if len(text) < 8 or len(src) < 2:
            continue
        out.append({"text": text, "sources": src, "ts": time.time()})
    _olds = _rejected(mem)
    written: list[dict] = []
    for it in out:
        if _too_similar(it["text"], _olds):
            continue          # 被用户否定过的判断，不再写回来
        try:
            vec = await mem.embed([it["text"]])
            doc_id = mem.add_document(
                it["text"], "insight", 0.5, ["理解"], [],
                vec=(vec[0] if vec else None),
                mtype="insight", sources=it["sources"])
            if doc_id:
                written.append({**it, "doc_id": doc_id})
        except Exception:  # noqa: BLE001
            continue
    return written
