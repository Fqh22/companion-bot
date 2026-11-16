"""[orchestration] 影子模式：只记录、不发送。用来在改行为前先量收益。

现在记五种（都只落盘、不改行为、不影响发送）：

1. **正例**——他说「哈哈哈」「笑死」或者接着追问，是正反馈。
   之前只有纠错（负例）闭环，正例全丢了。
2. **召回门控**——这轮判「要不要回忆」+ 实际召回条数（判据截止 2026-11-30）。
3. **碎片候选**——写入侧命中高相似时记一条，供离线评审（截止 2026-11-30）。
4. **连边扩展**——顺着「共同实体」能连到哪些没被召回的记录。
5. **二次召回**——用上一轮的话再召回，捞到了什么。

2026-11-16 删掉三个「无消费方」的影子：补一句 / 画像块 / 自指率。
补一句从没产生过样本（0 条），画像与自指率攒了 200+ 条但没有任何结论、
也没有人定期看，留着只是每轮多写一行盘。代码在 git 历史里，要重开再捡。
BM25+RRF 的影子同批删除（判据 11-14 已被人工否决）。
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from app_core import paths as _paths

log = logging.getLogger("qqbot.shadow")

DATA = _paths.DATA
POSITIVE_LOG = DATA / "shadow-positive.jsonl"
GRAPH_LOG = DATA / "shadow-graph.jsonl"
SECOND_LOG = DATA / "shadow-second.jsonl"
GATE_LOG = DATA / "shadow-recall-gate.jsonl"
DEDUP_LOG = DATA / "consolidation-candidates.jsonl"

# 「他接住了」的信号。宁窄勿宽：只在明显是笑/认同/继续追问时记。
_POSITIVE = ("哈哈", "笑死", "太对了", "绝了", "确实是这样", "你说得对",
             "我去试试", "有意思", "好家伙", "绷不住", "乐了", "懂了")


def positive_signal(user_text: str) -> bool:
    t = (user_text or "").strip()
    return bool(t) and any(w in t for w in _POSITIVE)


# 影子日志只用于排障/量化，不需要长期留存。单文件超过 5MB 就轮转成 .1，
# 旧的那份直接覆盖——最多两份，总占用有上限。
MAX_BYTES = 5 * 1024 * 1024


def _append(path: Path, rec: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if path.exists() and path.stat().st_size > MAX_BYTES:
                path.replace(path.with_suffix(path.suffix + ".1"))
        except Exception as e:  # noqa: BLE001
            log.debug("影子日志轮转失败：%s", e)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:  # noqa: BLE001
        log.debug("影子日志写入失败：%s", e)


def record_positive(user_text: str, reply: str) -> None:
    """记录「他这句是正反馈 + 上一条回复是什么」。只写文件。"""
    if not positive_signal(user_text):
        return
    _append(POSITIVE_LOG, {"ts": time.time(), "said": user_text[:120],
                           "reply": (reply or "")[:200]})


def record_dedup_hit(new_text: str, old_id: str, old_text: str,
                     cos: float) -> None:
    """记一次「本可合并的碎片」，供 ops/consolidate.py 离线评审。

    写入侧的影子去重以前只打一行 log，日志轮转后就没了，攒不出候选集。
    这里只落盘、**不改任何记忆**：归档 / 合并是不可逆的，按 01 §6 先有
    可审查的候选，截止 2026-11-30 再决定要不要做（docs/04）。
    """
    _append(DEDUP_LOG, {
        "ts": time.time(),
        "cos": round(float(cos), 3),
        "new": (new_text or "")[:120],
        "old_id": str(old_id or "")[:40],
        "old_text": (old_text or "")[:120],
    })


def record_recall_gate(query: str, user_text: str, need: bool, why: str,
                       recalled_n: int, mode: str = "shadow") -> None:
    """记一次「这轮要不要回忆」的判断 + 实际召回条数。只写文件，不改回复。

    判据刻意不用重合度那类代理指标（11-14 已被证伪）：要看的是端到端的
    「判定不需要的轮次里，召回的记忆有没有被用上」，配合 memory_trace
    的 injected 明细与重复告知率一起算，见 docs/04-评测与实验.md。
    """
    _append(GATE_LOG, {
        "ts": time.time(),
        "mode": mode,
        "need": bool(need),
        "why": why,
        "q": (query or "")[:60],
        "said": (user_text or "")[:80],
        "recalled": int(recalled_n or 0),
    })


def record_graph(query: str, dense: list[str], expanded: list[str]) -> None:
    """记一次「顺着共同实体能连到哪些没被召回的记录」。

    记的是**新增的**邻居而不是全部：这些候选一旦被采用，就是排在召回结果
    后面的「相关但没命中」。没有邻居时也记一条，否则算不出覆盖率。
    """
    d = [str(x)[:8] for x in (dense or [])][:5]
    e = [str(x)[:8] for x in (expanded or []) if str(x) not in set(dense or [])][:5]
    _append(GRAPH_LOG, {
        "ts": time.time(),
        "q": (query or "")[:60],
        "dense": d,
        "expanded": e,
    })


def record_second_recall(user_text: str, prev_text: str, rows: list[dict]) -> None:
    """记一次「用上一轮的话再召回，捞到了什么」。不改变本轮注入的记忆。"""
    _append(SECOND_LOG, {
        "ts": time.time(),
        "said": (user_text or "")[:80],
        "prev": (prev_text or "")[:80],
        "got": [str(r.get("doc_id"))[:8] for r in (rows or [])][:3],
        "texts": [(r.get("text") or "")[:40] for r in (rows or [])][:3],
    })


# ---------- 周汇总：把样本变成几句话 ----------
LOGS = {
    "positive": POSITIVE_LOG,
    "graph": GRAPH_LOG,
    "second": SECOND_LOG,
}


def _read_recent(path: Path, days: float, limit: int = 4000) -> list[dict]:
    """读最近 N 天的记录。只扫最后 limit 行，文件再大也不会拖慢周报。"""
    cutoff = time.time() - float(days) * 86400
    out: list[dict] = []
    try:
        if not path.exists():
            return []
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()[-limit:]
    except Exception as e:  # noqa: BLE001
        log.debug("影子日志读取失败 %s：%s", path.name, e)
        return []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        if isinstance(rec, dict) and float(rec.get("ts") or 0) >= cutoff:
            out.append(rec)
    return out


def _mean(xs: list[float]) -> float:
    return round(sum(xs) / len(xs), 2) if xs else 0.0


def weekly_summary(days: float = 7) -> dict:
    """把三路影子样本压成一份字典。纯计算，不写文件、不发消息。"""
    hit = {k: _read_recent(p, days) for k, p in LOGS.items()}
    po = hit["positive"]
    gr = hit["graph"]
    se = hit["second"]
    expanded = [len(x.get("expanded") or []) for x in gr]
    return {
        "days": days,
        "positive": {"n": len(po),
                     "sample": [str(x.get("said") or "")[:16] for x in po[-3:]]},
        "graph": {"n": len(gr),
                  "with_neighbors": sum(1 for x in expanded if x > 0),
                  "avg_expanded": _mean([float(x) for x in expanded])},
        "second": {"n": len(se),
                   "hit": sum(1 for x in se if x.get("got")),
                   "hit_rate": (round(100.0 * sum(1 for x in se if x.get("got")) / len(se))
                                if se else 0)},
    }


SNAP = DATA / "shadow-weekly.json"


def rotate_snapshot(curr: dict | None = None, now: float | None = None) -> dict:
    """把本周汇总存盘，返回**上一次**的汇总（给本周做对比）。

    为什么要留上一周：影子指标是缓慢漂移的，单看一周的数字没有意义——
    「二次召回命中 40%」是好是坏，得跟上周比。没有基线，指标就只是装饰。
    """
    now = time.time() if now is None else now
    prev: dict = {}
    try:
        if SNAP.exists():
            prev = json.loads(SNAP.read_text(encoding="utf-8")) or {}
    except Exception as e:  # noqa: BLE001
        log.debug("影子周快照读取失败：%s", e)
    cur = curr if curr is not None else weekly_summary(7)
    try:
        SNAP.parent.mkdir(parents=True, exist_ok=True)
        SNAP.write_text(json.dumps({"at": now, "summary": cur}, ensure_ascii=False),
                        encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        log.debug("影子周快照写入失败：%s", e)
    return (prev or {}).get("summary") or {}


def compare_lines(curr: dict, prev: dict) -> list[str]:
    """本周 vs 上周。只报有数的项，差值保留两位。"""
    if not prev:
        return []
    out: list[str] = []

    def one(label: str, a, b, unit: str = "") -> None:
        if a is None or b is None:
            return
        try:
            d = float(a) - float(b)
        except Exception:  # noqa: BLE001
            return
        out.append("%s：%s%s（上周 %s%s，%+.2f）" % (label, a, unit, b, unit, d))

    one("二次召回命中", curr.get("second", {}).get("hit_rate"),
        prev.get("second", {}).get("hit_rate"), "%")
    one("连边覆盖率", (round(100.0 * curr["graph"]["with_neighbors"] / curr["graph"]["n"], 1)
                   if curr.get("graph", {}).get("n") else None),
        (round(100.0 * prev["graph"]["with_neighbors"] / prev["graph"]["n"], 1)
         if prev.get("graph", {}).get("n") else None), "%")
    return out


def summary_lines(days: float = 7, prev: dict | None = None) -> list[str]:
    """周报里那几行。没数据的档位直接不出现，别拿空行凑数。"""
    s = weekly_summary(days)
    out: list[str] = []
    g = s["graph"]
    if g["n"]:
        out.append("实体连边 %d 次：有邻居的 %d 次（%.0f%%），平均新增 %.1f 条"
                   % (g["n"], g["with_neighbors"],
                      100.0 * g["with_neighbors"] / g["n"], g["avg_expanded"]))
    sec = s["second"]
    if sec["n"]:
        out.append("二次召回 %d 次：捞到东西 %d 次（%d%%）"
                   % (sec["n"], sec["hit"], sec["hit_rate"]))
    pos = s["positive"]
    if pos["n"]:
        out.append("正反馈 %d 条" % pos["n"])
    if prev:
        cmp_ = compare_lines(s, prev)
        if cmp_:
            out.append("与上周相比：" + "；".join(cmp_))
    return out
