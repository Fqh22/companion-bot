"""矛盾检测：新记忆会不会让旧记忆过时。

**为什么要做**：记忆是只增不减的事实集合——「他在找工作」和「他上周入职了」
会同时留在库里。召回按相似度取，两条都会被选中，模型看到互相矛盾的两条，
只能猜；表现出来就是「它记混了」。

**做法**：新记忆写入时，找几条高度相似的活跃旧记忆，问一次模型「是不是同一件事
的两个版本」；是就把旧的标成 superseded（软删，可恢复）。

**保守是有意的**：误杀一条真记忆比漏判一次矛盾更糟。所以四道闸：
1. 相似度 >= CAND_MIN_SCORE 才去问（不相似的多半不是同一件事，白花钱还容易误判）
2. 模型必须给出理由，且相似度 >= SUPERSEDE_MIN_SCORE 才真的改
3. 每次最多改 MAX_PER_RUN 条（一次总结不该抹掉一串）
4. 只改状态不删数据：/memory_restore 能捞回来，每条决定都落 jsonl

模式由 memory.contradiction_check 控制：off / shadow（只记录）/ on。
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

CAND_MIN_SCORE = 0.50
SUPERSEDE_MIN_SCORE = 0.60
MAX_CAND = 3
MAX_PER_RUN = 2

LOG = Path("/opt/nonebot/data/contradictions.jsonl")

SYSTEM = (
    "你在核对记忆有没有自相矛盾——同一件事的两个版本。只判「不能同时成立」的。"
    "补充信息、相关话题、情绪变化都不算矛盾。"
    "矛盾（旧的已经过时，该被新的取代）：「他在找工作」和「他上周入职了」、"
    "「他养猫」和「他家现在只有一只狗」。"
    "不矛盾（可以同时成立）：「他在做机器人」和「他在学 Rust」、"
    "「他喜欢美式」和「他今天喝了拿铁」。"
    "只有旧的被新的**明确取代**时才写 supersede，拿不准一律 keep。"
    '输出 JSON，不要别的：{"pairs":[{"old":"旧记忆编号","action":"supersede|keep",'
    '"why":"不超过 15 字的理由"}]}。没有矛盾就输出 {"pairs":[]}。'
)


def build_messages(summary: str, key_facts: list[str] | None, cands: list[dict]) -> list[dict]:
    """拼一次判定请求。cands 是 [{"doc_id","text","score"}]。"""
    facts = "\n".join(f"- {x}" for x in (key_facts or [])[:5])
    old = "\n".join(f"[{c['doc_id']}] {str(c.get('text') or '')[:220]}" for c in cands)
    user = (
        "【新记忆】\n" + str(summary or "")[:700] + "\n"
        + ("\n关键事实：\n" + facts + "\n" if facts else "")
        + "\n【旧记忆（可能已经过时）】\n" + old
    )
    return [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": user}]


def parse(raw: str) -> list[dict]:
    """解析模型输出。容忍代码围栏、多余的说明文字、裸数组。"""
    t = (raw or "").strip()
    fence = chr(96) * 3          # 代码围栏，模型偶尔会包一层
    if t.startswith(fence):
        t = t[len(fence):]
        if t[:4].lower() == "json":
            t = t[4:]
    j = t.rfind(fence)
    if j >= 0:
        t = t[:j]
    t = t.strip()
    k, l = t.find("{"), t.rfind("}")
    i, j2 = t.find("["), t.rfind("]")
    # 谁先出现就按谁截：裸数组的第一个 '{' 在数组内部，先判 { 会把数组切坏
    if i >= 0 and j2 > i and (k < 0 or i < k):
        t = t[i:j2 + 1]
    elif k >= 0 and l > k:
        t = t[k:l + 1]
    try:
        data = json.loads(t)
    except Exception:  # noqa: BLE001
        return []
    pairs = data.get("pairs") if isinstance(data, dict) else data
    if not isinstance(pairs, list):
        return []
    out: list[dict] = []
    for p in pairs:
        if not isinstance(p, dict):
            continue
        old = str(p.get("old") or "").strip()
        act = str(p.get("action") or "").strip().lower()
        if not old or act not in ("supersede", "keep"):
            continue
        out.append({"old": old, "action": act,
                    "why": " ".join(str(p.get("why") or "").split())[:40]})
    return out


def should_supersede(pair: dict, score: float, applied: int,
                     cap: int = MAX_PER_RUN,
                     min_score: float = SUPERSEDE_MIN_SCORE) -> tuple[bool, str]:
    """四道闸的最终判定。返回 (改不改, 原因)——原因要能写进日志。"""
    if pair.get("action") != "supersede":
        return False, "model-keep"
    if applied >= cap:
        return False, "cap-reached"
    if not str(pair.get("why") or "").strip():
        return False, "no-reason"
    if float(score or 0) < min_score:
        return False, "low-similarity"
    return True, "ok"


def record(pair: dict, cand: dict, applied: bool, reason: str,
           new_doc: str = "", mode: str = "on", ts: float | None = None) -> None:
    """把每一次判定写一行。写失败不抛——这段代码不该影响主流程。"""
    try:
        rec = {
            "ts": ts if ts is not None else time.time(),
            "mode": mode,
            "new": new_doc,
            "old": cand.get("doc_id"),
            "score": round(float(cand.get("score") or 0), 3),
            "action": pair.get("action"),
            "why": pair.get("why") or "",
            "applied": bool(applied),
            "reason": reason,
            "old_text": str(cand.get("text") or "")[:120],
        }
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        pass
