"""主题词表：让新记忆复用已有标签，而不是每篇自己造一个。

**要治的病（2026-11-05 实测）**：119 篇记忆里有 410 个不同的主题词，
其中 **393 个只出现过一次**。同一件事被拆成「harness」「harness 架构」
「DSH」三个标签，于是靠实体连边的 `memgraph` 只能连到 34% 的记忆，
关键词召回也捞不到稳定的一组。

修法分两半，这里是两半共用的纯逻辑：

1. **生成时给模型看词表** —— 摘要 prompt 里附上高频标签，让它优先复用
   （`chat.py` 调 `summarize.vocab_hint`）。
2. **落库前再归并一次** —— 模型偶尔还是会给「求职」和「求职面试」，
   这里按字面相似度合并到已有的那个（`canonicalize`）。

**为什么不用 textsim.similar**：那个函数是给「五字以上的句子」设计的，
短于 5 个字直接返回 0，宁可判不同也不误合并。标签恰恰都是 2–4 个字，
用它会一个都合不上。这里换一套：先看包含关系，再看字符 bigram 的 Jaccard。
"""
from __future__ import annotations

import re

_SPACE = re.compile(r"\s+")


def _norm(topic: str) -> str:
    return _SPACE.sub("", str(topic or "")).lower().strip("，,。.;；:：、")


def similarity(a: str, b: str) -> float:
    """标签相似度：完全相同 1.0；包含 0.9；否则 char-bigram Jaccard。"""
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    if na in nb or nb in na:
        return 0.9
    ba = {na[i:i + 2] for i in range(len(na) - 1)}
    bb = {nb[i:i + 2] for i in range(len(nb) - 1)}
    if not ba or not bb:
        return 0.0
    return len(ba & bb) / len(ba | bb)


def build(topic_lists, limit: int = 50) -> list[str]:
    """从历史标签里取高频词，作为「该复用哪些」的候选表。

    按归一化后的写法计数，输出用出现最多的那个写法（surface form），
    这样 prompt 里给模型看到的是人话，不是小写串。
    """
    counts: dict[str, int] = {}
    surface: dict[str, str] = {}
    for topics in topic_lists or []:
        for t in topics or []:
            key = _norm(t)
            if not key:
                continue
            counts[key] = counts.get(key, 0) + 1
            if key not in surface or len(str(t)) < len(surface[key]):
                surface[key] = str(t).strip()
    top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
    return [surface[k] for k, _ in top]


def canonicalize(topics, vocab, threshold: float = 0.75,
                 limit: int = 4) -> list[str]:
    """把新标签映射到已有词表上；对不上的保留原样。

    返回最多 `limit` 个、按原顺序去重后的标签。
    """
    out: list[str] = []
    seen: set[str] = set()
    vocab = [v for v in (vocab or []) if _norm(v)]
    for t in topics or []:
        t = str(t).strip()
        if not t:
            continue
        pick = t
        best, best_sim = 0.0, ""
        for v in vocab:
            s = similarity(t, v)
            if s > best:
                best, best_sim = s, v
        if best >= threshold:
            pick = best_sim
        key = _norm(pick)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(pick)
        if len(out) >= limit:
            break
    return out
