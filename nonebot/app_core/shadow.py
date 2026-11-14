"""影子模式：只记录、不发送。用来在改行为前先量收益。

现在记三种：

1. **补一句**——回复发完之后，本来可以再补一句（真人常有）。
   这里只生成候选并落盘，不发。攒够样本再看「有多少是真值得发」。
2. **正例**——他说「哈哈哈」「笑死」或者接着追问，是正反馈。
   之前只有纠错（负例）闭环，正例全丢了。
3. **召回对比**——dense 召回的 top-k 与 BM25+RRF 的 top-k 各是什么，
   重合几个。决定要不要把关键词通道转正，靠的就是这批样本。
4. **连边扩展**——召回之后，顺着「共同实体」能连到哪些没被召回的记录。
   用来判断「关于这件事还聊过什么」这条路径到底有没有料。

都不改现有行为，也不影响发送。
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from app_core import paths as _paths

log = logging.getLogger("qqbot.shadow")

DATA = _paths.DATA
FOLLOWUP_LOG = DATA / "shadow-followup.jsonl"
POSITIVE_LOG = DATA / "shadow-positive.jsonl"
RETRIEVAL_LOG = DATA / "shadow-retrieval.jsonl"
GRAPH_LOG = DATA / "shadow-graph.jsonl"
PROFILE_LOG = DATA / "shadow-profile.jsonl"
SECOND_LOG = DATA / "shadow-second.jsonl"
ATTENTION_LOG = DATA / "shadow-attention.jsonl"

FOLLOWUP_PROMPT = (
    "你刚回了下面这句话。现在像突然想起什么，再补一句不超过 20 个字的话。\n"
    "可以是吐槽、补充、或者没头没尾的一小句；不要建议，不要总结，不要问句。\n"
    "只输出这句话本身。"
)

# 「他接住了」的信号。宁窄勿宽：只在明显是笑/认同/继续追问时记。
_POSITIVE = ("哈哈", "笑死", "太对了", "绝了", "确实是这样", "你说得对",
             "我去试试", "有意思", "好家伙", "绷不住", "乐了", "懂了")


def positive_signal(user_text: str) -> bool:
    t = (user_text or "").strip()
    return bool(t) and any(w in t for w in _POSITIVE)


# 影子日志只用于排障/量化，不需要长期留存。单文件超过 5MB 就轮转成 .1，
# 旧的那份直接覆盖——最多两份，总占用有上限（实测 shadow-profile 11 天 762KB）。
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


def record_retrieval(query: str, dense: list[str], bm25: list[str],
                     fused: list[str]) -> None:
    """记一次「dense 召回 vs BM25+RRF 召回」。只写文件，不改变任何返回。

    存的是两路各自的名单而不是一个差异数：事后要能回答
    「被换掉的是哪条、换上来的是哪条」，只记 overlap 就查不了。
    doc_id 截 8 位，跟 memory 的召回日志同一个精度。
    """
    d = [str(x)[:8] for x in (dense or [])][:5]
    f = [str(x)[:8] for x in (fused or [])][:5]
    _append(RETRIEVAL_LOG, {
        "ts": time.time(),
        "q": (query or "")[:60],
        "dense": d,
        "bm25": [str(x)[:8] for x in (bm25 or [])][:5],
        "fused": f,
        "overlap": len(set(d) & set(f)),
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


async def record_followup(llm, user_text: str, reply: str) -> str:
    """生成「本来可以补的那一句」，落盘，**不返回给发送层**。"""
    if not (user_text or "").strip() or not (reply or "").strip():
        return ""
    try:
        msg = await llm.complete(
            [{"role": "system", "content": "你说话很短，像在 QQ 上打字。"},
             {"role": "user",
              "content": f"{FOLLOWUP_PROMPT}\n\n他说的：{user_text[:200]}\n"
                         f"你回的：{reply[:300]}"}],
            None, max_tokens=60, thinking=False, purpose="shadow")
        cand = (msg.get("content") or "").strip().replace("\n", " ")
    except Exception as e:  # noqa: BLE001
        log.debug("影子补句生成失败：%s", e)
        return ""
    if len(cand) < 6:                      # 太短没意义，不记
        return ""
    _append(FOLLOWUP_LOG, {"ts": time.time(), "said": user_text[:120],
                           "reply": (reply or "")[:200], "candidate": cand[:80]})
    return cand


_LAST_PROFILE: dict = {}


def record_profile(block: str, fields: dict, mode: str = "shadow") -> None:
    """记一次「如果注入了画像，会注入这些话」。只写文件，不改 messages。

    mode=on 时也记一条（带 mode 字段），这样切开数据能对比注入前后。

    **只在变化时写**：画像一周才更新一次，之前每轮都写一遍相同的 197 字，
    4.5 小时攒了 120 条 99% 重复的日志——既浪费 I/O，也让人看不出它到底什么时候变过。
    现在同一进程内相同 block 只记一次，重启后第一条仍会记（记录当前状态）。
    """
    if not block:
        return
    _key = (block[:240], mode)
    if _LAST_PROFILE.get("key") == _key:
        return
    _LAST_PROFILE["key"] = _key
    _append(PROFILE_LOG, {
        "ts": time.time(),
        "mode": mode,
        "block": block[:240],
        "fields": {k: str(v)[:60] for k, v in (fields or {}).items()},
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


def record_attention(reply: str, ratio: float | None, self_n: int, you_n: int,
                     user_text: str = "") -> None:
    """记一条「这条回复有多少在说他、多少在说自己」。只写文件。"""
    if ratio is None:
        return
    _append(ATTENTION_LOG, {
        "ts": time.time(),
        "ratio": ratio,
        "self": self_n,
        "you": you_n,
        "len": len(reply or ""),
        "said": (user_text or "")[:60],
        "reply": (reply or "")[:80],
    })


# ---------- 周汇总：把七路样本变成几句话 ----------
# 影子的价值在「先量再改」，但如果没人汇总，样本就只是躺在磁盘上。
# 每周日写周报时顺手算一遍：命中率、重合度、覆盖率、自指率，各一行。

LOGS = {
    "followup": FOLLOWUP_LOG,
    "positive": POSITIVE_LOG,
    "retrieval": RETRIEVAL_LOG,
    "graph": GRAPH_LOG,
    "profile": PROFILE_LOG,
    "second": SECOND_LOG,
    "attention": ATTENTION_LOG,
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


# BM25+RRF 影子的判据：起跑两周必须做决定，不许变成永远的影子。
# 重合 ≥4.0 → 两路几乎一样，砍；≤3.0 → 互补明显，转正；中间人工抽 20 条。
RETRIEVAL_DECIDE_DAYS = 14
RETRIEVAL_MIN_SAMPLES = 50
RETRIEVAL_CUT_OVERLAP = 4.0
RETRIEVAL_KEEP_OVERLAP = 3.0


def retrieval_verdict(now: float | None = None) -> dict:
    """BM25 影子跑到哪一步、该不该做决定了。

    影子模式的初衷是「改行为前先量收益」；量到了就必须改，否则它只是
    占着写盘和注意力、却从不影响召回的死代码。
    """
    now = time.time() if now is None else now
    # 判据看全量样本（不按时间窗过滤）：影子可能只在早期跑过，
    # 用「最近 N 天」会把结论算成「没数据」。
    rows: list[dict] = []
    try:
        with RETRIEVAL_LOG.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh.readlines()[-4000:]:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                if isinstance(rec, dict):
                    rows.append(rec)
    except Exception:  # noqa: BLE001
        rows = []
    n = len(rows)
    if not n:
        return {"n": 0, "days": 0.0, "avg_overlap": 0.0, "due": False,
                "verdict": "还没有样本"}
    first = min(float(r.get("ts") or now) for r in rows)
    days = (now - first) / 86400.0
    avg = _mean([float(r.get("overlap") or 0) for r in rows])
    due = days >= RETRIEVAL_DECIDE_DAYS
    if not due or n < RETRIEVAL_MIN_SAMPLES:
        verdict = f"继续攒样本（{n}/{RETRIEVAL_MIN_SAMPLES} 次，第 {days:.0f} 天）"
    elif avg >= RETRIEVAL_CUT_OVERLAP:
        verdict = "建议砍掉：两路几乎一样，融合没有收益"
    elif avg <= RETRIEVAL_KEEP_OVERLAP:
        verdict = "建议转正：互补明显，值得改召回"
    else:
        verdict = "人工抽检 20 条 query 再定"
    return {"n": n, "days": round(days, 1), "avg_overlap": avg,
            "due": due, "verdict": verdict}


def retrieval_line(now: float | None = None) -> str:
    v = retrieval_verdict(now)
    if not v["n"]:
        return "BM25 影子：还没有样本"
    return (f"BM25 影子：{v['n']} 次，重合 {v['avg_overlap']:.1f}/5，第 {v['days']:.0f} 天"
            f"（判据 {RETRIEVAL_DECIDE_DAYS} 天 / {RETRIEVAL_MIN_SAMPLES} 次）"
            f" → {v['verdict']}")


def _mean(xs: list[float]) -> float:
    return round(sum(xs) / len(xs), 2) if xs else 0.0


def weekly_summary(days: float = 7) -> dict:
    """把七路影子样本压成一份字典。纯计算，不写文件、不发消息。"""
    hit = {k: _read_recent(p, days) for k, p in LOGS.items()}

    fu = hit["followup"]
    po = hit["positive"]
    re_ = hit["retrieval"]
    gr = hit["graph"]
    pf = hit["profile"]
    se = hit["second"]
    at = hit["attention"]

    overlaps = [float(x.get("overlap") or 0) for x in re_]
    expanded = [len(x.get("expanded") or []) for x in gr]
    ratios = [float(x.get("ratio") or 0) for x in at
              if isinstance(x.get("ratio"), (int, float))]
    half = len(ratios) // 2
    trend = None
    if len(ratios) >= 6:
        trend = round(_mean(ratios[half:]) - _mean(ratios[:half]), 2)

    modes: dict[str, int] = {}
    for x in pf:
        m = str(x.get("mode") or "?")
        modes[m] = modes.get(m, 0) + 1

    return {
        "days": days,
        "followup": {"n": len(fu),
                     "avg_len": _mean([len(str(x.get("candidate") or "")) for x in fu]),
                     "sample": [str(x.get("candidate") or "")[:24] for x in fu[-3:]]},
        "positive": {"n": len(po),
                     "sample": [str(x.get("said") or "")[:16] for x in po[-3:]]},
        "retrieval": {"n": len(re_),
                      "avg_overlap": _mean(overlaps),
                      "disjoint": sum(1 for x in overlaps if x == 0)},
        "graph": {"n": len(gr),
                  "with_neighbors": sum(1 for x in expanded if x > 0),
                  "avg_expanded": _mean([float(x) for x in expanded])},
        "profile": {"n": len(pf), "by_mode": modes,
                    "avg_len": _mean([len(str(x.get("block") or "")) for x in pf])},
        "second": {"n": len(se),
                   "hit": sum(1 for x in se if x.get("got")),
                   "hit_rate": (round(100.0 * sum(1 for x in se if x.get("got")) / len(se))
                                if se else 0)},
        "attention": {"n": len(ratios), "avg_ratio": _mean(ratios), "trend": trend},
    }


SNAP = DATA / "shadow-weekly.json"


def rotate_snapshot(curr: dict | None = None, now: float | None = None) -> dict:
    """把本周汇总存盘，返回**上一次**的汇总（给本周做对比）。

    为什么要留上一周：影子指标是缓慢漂移的，单看一周的数字没有意义——
    「召回重合 2.1」是好是坏，得跟上周的 1.8 比。没有基线，指标就只是装饰。
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

    one("召回平均重合", curr.get("retrieval", {}).get("avg_overlap"),
        prev.get("retrieval", {}).get("avg_overlap"))
    one("二次召回命中", curr.get("second", {}).get("hit_rate"),
        prev.get("second", {}).get("hit_rate"), "%")
    one("自指率", curr.get("attention", {}).get("avg_ratio"),
        prev.get("attention", {}).get("avg_ratio"))
    one("连边覆盖率", (round(100.0 * curr["graph"]["with_neighbors"] / curr["graph"]["n"], 1)
                   if curr.get("graph", {}).get("n") else None),
        (round(100.0 * prev["graph"]["with_neighbors"] / prev["graph"]["n"], 1)
         if prev.get("graph", {}).get("n") else None), "%")
    return out


def summary_lines(days: float = 7, prev: dict | None = None) -> list[str]:
    """周报里那几行。没数据的档位直接不出现，别拿空行凑数。"""
    s = weekly_summary(days)
    out: list[str] = []
    r = s["retrieval"]
    if r["n"]:
        out.append("召回对比 %d 次：dense 与 BM25 平均重合 %.1f/5，完全不同的 %d 次"
                   % (r["n"], r["avg_overlap"], r["disjoint"]))
    g = s["graph"]
    if g["n"]:
        out.append("实体连边 %d 次：有邻居的 %d 次（%.0f%%），平均新增 %.1f 条"
                   % (g["n"], g["with_neighbors"],
                      100.0 * g["with_neighbors"] / g["n"], g["avg_expanded"]))
    sec = s["second"]
    if sec["n"]:
        out.append("二次召回 %d 次：捞到东西 %d 次（%d%%）"
                   % (sec["n"], sec["hit"], sec["hit_rate"]))
    a = s["attention"]
    if a["n"]:
        line = "自指率 %.2f（%d 条）" % (a["avg_ratio"], a["n"])
        if a["trend"] is not None:
            line += "，后半周比前半周 %+.2f" % a["trend"]
        out.append(line)
    p = s["profile"]
    if p["n"]:
        modes = "、".join("%s %d" % (k, v) for k, v in sorted(p["by_mode"].items()))
        out.append("画像块 %d 次（%s），平均 %.0f 字" % (p["n"], modes, p["avg_len"]))
    f = s["followup"]
    if f["n"]:
        out.append("补一句候选 %d 条，平均 %.0f 字" % (f["n"], f["avg_len"]))
    pos = s["positive"]
    if pos["n"]:
        out.append("正反馈 %d 条" % pos["n"])
    if prev:
        cmp_ = compare_lines(s, prev)
        if cmp_:
            out.append("与上周相比：" + "；".join(cmp_))
    return out

