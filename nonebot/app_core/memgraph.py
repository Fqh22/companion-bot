"""记忆连边：靠「共同实体」把同一件事的不同记录串起来。

**要解决什么**：dense 召回只能回答「哪条最像这句 query」，回答不了
「关于这件事，我们还聊过什么」。一次装机咨询会散成四五条记忆，
问「内存多少钱」时只捞到最像的那一条，其余几条明明相关却不出现。

**怎么连**：`memory_atoms.entities` 是抽取时标好的标签，两条记忆共享
同一个实体就建一条边。边的权重用 idf：越少见的实体连得越紧，
「装机配置」这种 6 条都有的标签会被稀释，不会把整团都拉进来。

**2026-11-05 旧快照**：119 篇文档 / 615 条原子，文档级实体 410 个，
**393 个只出现在一篇里**，df≥2 的只有 17 个；当时 34% 的记忆能连到邻居，平均 0.89 个。
**2026-11-10 影子**：333 次里 99% 有邻居，平均新增 5 条——图变胖了，但这是覆盖率，
不是精度。根因仍在写入侧词表；影子只回答「连出来的邻居有没有用」。
"""
from __future__ import annotations

from app_core import vocab

import math
import re

# 超过这个比例的文档都有的实体当停用标签（会连出整团）。
# 0.25 是实测定的：119 篇文档下，0.20 会把「记忆系统」（24 篇）这类
# 真·主题词误剪掉，0.25 之后有邻居的文档从 108 升到 114，再往上（0.3、1.0）
# 结果完全一样——说明有意义的 hub 到这就收齐了。
MAX_DF_RATIO = 0.25

_SPACE = re.compile(r"\s+")


def normalize(entity: str) -> str:
    """实体名的规范形：去掉所有空白、斜杠两侧拉齐、统一小写。

    「harness 架构」和「harness架构」得算同一个，否则同一件事会被拆成两条边。
    """
    s = _SPACE.sub("", str(entity or "")).lower()
    s = s.replace("／", "/").strip("，,。.;；")
    # 别名表：让「harness 架构」和「DSH」在连边时算同一个实体。
    # 放在读侧归一，老数据不用迁移就能立刻受益。
    return vocab._norm(vocab.alias(s))


class EntityGraph:
    """实体倒排 + 共同实体加权。纯内存，119 篇文档建一次不到 1 ms。

    调用方负责按「库变了就重建」缓存实例。
    """

    __slots__ = ("n", "entity_df", "doc_entities", "inverted", "idf", "usable")

    def __init__(self, rows: list[tuple[int, list[str]]],
                 max_df_ratio: float = MAX_DF_RATIO):
        per: dict[int, set[str]] = {}
        for parent, entities in rows:
            es = {normalize(e) for e in (entities or []) if str(e).strip()}
            es.discard("")
            if es:
                per[parent] = per.get(parent, set()) | es
        self.doc_entities = per
        self.n = len(per)

        df: dict[str, int] = {}
        for es in per.values():
            for e in es:
                df[e] = df.get(e, 0) + 1
        self.entity_df = df

        # df=1 的实体连不到任何东西（只在一篇里出现），df 太大的会连出整团。
        # 两头都剪掉，否则「图片识别」这类标签会把一半记忆拉进候选。
        cap = max(2, int(self.n * max_df_ratio))
        self.usable = {e for e, v in df.items() if 2 <= v <= cap}
        self.idf = {
            e: math.log(1.0 + (self.n - df[e] + 0.5) / (df[e] + 0.5))
            for e in self.usable
        }

        inv: dict[str, set[int]] = {}
        for parent, es in per.items():
            for e in es:
                if e in self.usable:
                    inv.setdefault(e, set()).add(parent)
        self.inverted = inv

    def related(self, parents: list[int], limit: int = 5) -> list[tuple[int, float]]:
        """给定若干「种子记忆」，返回它们的邻居（不含种子自己），按边权降序。

        边权 = Σ idf(共同实体) / √df。除以 √df 是因为大标签天然会拉一大片，
        不压一下，「装机配置」六条会永远霸占每个装机关联的候选位。
        """
        seeds = [p for p in parents if p in self.doc_entities]
        if not seeds or not self.inverted:
            return []
        seed_set = set(seeds)
        score: dict[int, float] = {}
        for p in seeds:
            for e in self.doc_entities.get(p, ()):
                if e not in self.usable:
                    continue
                peers = self.inverted.get(e, ())
                if len(peers) < 2:
                    continue
                w = self.idf[e] / math.sqrt(len(peers))
                for q in peers:
                    if q in seed_set:
                        continue
                    score[q] = score.get(q, 0.0) + w
        out = sorted(score.items(), key=lambda kv: (-kv[1], kv[0]))
        return out[:limit]

    def stats(self) -> dict:
        return {"docs": self.n, "entities": len(self.entity_df),
                "usable": len(self.usable)}
