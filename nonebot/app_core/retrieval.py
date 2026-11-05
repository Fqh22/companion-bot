"""关键词检索：BM25 + RRF 融合（纯逻辑，只依赖标准库）。

**为什么需要它**：`memory.recall` 现在只有一条 dense 通道（矩阵余弦）。
向量擅长「意思相近」，不擅长「一模一样的 token」——QQ 号、API 名、日期、
代码里的标识符，在向量空间里和一堆闲聊挤在一起，召回时常被
「意思接近但事实无关」的句子顶掉。BM25 补的正是这一路：
词面命中就给分，不管语义。

**中文怎么切**：不引 jieba。云端只有一个 venv，多一个依赖就多一处部署风险，
而 jieba 的词典要十几 MB 常驻内存。这里用**字符 bigram**——
「今天天气」切成 今天/天天/天气。跟 textsim.similar 里 bigram 覆盖率的
直觉一致，也不需要词表；代价是索引项比词级多 2–3 倍（单用户机器人可忽略）。
纯 ASCII 的词（deepseek、sesskey、10000）保持整 token，不切。

**为什么融合用 RRF 而不是加权求和**：cos ∈ [-1,1]，BM25 无上界，
min-max 归一化会被单条极值带偏；候选在某一路缺席时记 0 分，等于变相惩罚。
RRF 只看名次（1/(k+rank)），天然免疫量纲问题。

**这一版只做影子**：`memory.recall` 算完 dense 结果后顺带算一遍 BM25+RRF，
落盘对比，**不改返回值**。攒够样本再决定要不要真的切过去。
"""
from __future__ import annotations

import math
import re

# ASCII 词 / 数字整体保留；CJK 连续段按字符 bigram 切。
_TOKEN_RE = re.compile(r"[a-z0-9_]+|[\u4e00-\u9fff]+")

# RRF 的平滑常数。60 是原论文（Cormack et al. 2009）的经验值：
# 越大越平权（第一名优势越小），越小越信单路的第一名。
RRF_K = 60


def tokenize(text: str) -> list[str]:
    """切成检索项。ASCII 词整 token，中文按字 bigram，单字保留单字。

    不做去停用词：BM25 的 idf 本来就会把高频词的权重压到接近 0，
    多一张停用词表只是多一处要维护的东西。
    """
    s = (text or "").lower()
    out: list[str] = []
    for m in _TOKEN_RE.finditer(s):
        tok = m.group(0)
        if tok[0].isascii():
            out.append(tok)
        elif len(tok) == 1:
            out.append(tok)
        else:
            out.extend(tok[i:i + 2] for i in range(len(tok) - 1))
    return out


class BM25:
    """标准 Okapi BM25（k1=1.5, b=0.75，Lucene 默认档）。

    只建一次索引；调用方按「库的条数没变就复用」缓存它。
    纯 Python 实现：1000 条 × 100 词大约 30 ms 建索引，
    单次打分只看 query 命中的倒排链，是微秒级。
    """

    __slots__ = ("k1", "b", "n", "lens", "avgdl", "idf", "postings")

    def __init__(self, docs: list[str], k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self.n = len(docs)
        lens: list[int] = []
        postings: dict[str, list[tuple[int, int]]] = {}
        df: dict[str, int] = {}
        for i, doc in enumerate(docs):
            toks = tokenize(doc)
            lens.append(len(toks))
            if not toks:
                continue
            tf: dict[str, int] = {}
            for t in toks:
                tf[t] = tf.get(t, 0) + 1
            for t, c in tf.items():
                postings.setdefault(t, []).append((i, c))
                df[t] = df.get(t, 0) + 1
        self.lens = lens
        self.postings = postings
        self.avgdl = (sum(lens) / self.n) if self.n else 0.0
        # ln(1 + (N-df+0.5)/(df+0.5))：Lucene 的写法，恒为正，
        # 不会出现「出现率 50% 的词权重为 0」那种经典奇观。
        self.idf = {
            t: math.log(1.0 + (self.n - c + 0.5) / (c + 0.5)) for t, c in df.items()
        }

    def scores(self, query: str) -> list[float]:
        """每条文档一个分数，顺序与构造时的 docs 一致。全不命中就是 0。"""
        out = [0.0] * self.n
        terms = tokenize(query)
        if not terms or not self.n:
            return out
        avgdl = self.avgdl or 1.0
        for term in set(terms):          # 去重：query 里重复的词不重复加分
            idf = self.idf.get(term)
            if idf is None:
                continue
            for i, tf in self.postings.get(term, ()):
                dl = self.lens[i] or 1
                denom = tf + self.k1 * (1.0 - self.b + self.b * dl / avgdl)
                out[i] += idf * tf * (self.k1 + 1.0) / denom
        return out

    def ranked(self, query: str, limit: int = 20) -> list[int]:
        """按分数从高到低返回下标，只留正分。同分时按下标升序，保证可复现。"""
        sc = self.scores(query)
        idx = [i for i, s in enumerate(sc) if s > 0.0]
        idx.sort(key=lambda i: (-sc[i], i))
        return idx[:limit]


def rrf(rankings: list[list], k: int = RRF_K) -> dict:
    """Reciprocal Rank Fusion：对每个候选累加 1/(k+rank)。

    rankings 是若干「已排好序的列表」，元素可以是任意可哈希对象
    （这里用 doc_id 字符串）。名次从 1 开始。
    """
    out: dict = {}
    for ranking in rankings:
        for rank, item in enumerate(ranking, start=1):
            out[item] = out.get(item, 0.0) + 1.0 / (k + rank)
    return out


def fuse(dense_ids: list[str], bm25_ids: list[str], limit: int = 5,
         k: int = RRF_K) -> list[str]:
    """把两路排好的 doc_id 融成一个名次表。两路都没有的候选不会出现。"""
    if not bm25_ids:
        return dense_ids[:limit]
    if not dense_ids:
        return bm25_ids[:limit]
    fused = rrf([dense_ids, bm25_ids], k=k)
    return sorted(fused, key=lambda d: (-fused[d], d))[:limit]
