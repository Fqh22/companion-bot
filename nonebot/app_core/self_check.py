"""[domain] 回话自检标签的解析（docs/12）。

模型在正文前写一行：
    [自检] 主=接住 次=判断 置信=高 | 依据=有 来源=记忆

这里只做两件事：剥掉这一行、把标签留下。解析失败一律原样返回——
标签是观测手段，不能让它影响回复本身。
"""
from __future__ import annotations

import re

# 标签可能以换行结尾，也可能直接跟正文（中间两个空格、甚至没有空格）。
# 「来源=」的值只有几种，用白名单收尾——用 \S+ 会把正文一起吞掉
# （实测：来源=记忆我这边收到的还是文字）。
_SRC = r"(?:记忆|搜索|无|文件|记忆\+搜索|两者)"
_RE_FULL = re.compile(
    r"^\s*\[自检\]\s*(.+?来源\s*=\s*" + _SRC + r"(?:[（(][^）)]{0,30}[）)])?)\s*",
    re.S)
_RE_LINE = re.compile(r"^\s*\[自检\]\s*(.+?)\s*\n", re.S)
# 模型有时在标签后再写一行草稿：「|无」「+推理」。它不属于正文，
# 但 _RE_FULL 只剥到「来源=记忆」就停了，于是整条回复以「|无」开头
# （2026-11-13 第 3034/3042 条）。这里只清理正文最前面的这类标记。
_RE_DRAFT = re.compile(r"^(?:\|[^\n]{0,4}\n+|[+＋][^\n]{0,6}\n+|\|(?:无|有|记忆|搜索)|[+＋](?:推理|思考))\s*")
# 括号也可能写成「【自检】」或在括号里加空格：「[ 自检 ]」。
_RE_OPEN = re.compile(r"^\s*[\[【]\s*自\s*检\s*[\]】]")
# 残片：模型偶尔整段漏写「[自检] 主」，直接以「=接住 次=事实 … 来源=记忆」开头，
# 正文还紧贴其后。2026-11-14 线上第 3143 条整行被原样发给用户。
# 这里兜底：允许没有前缀，只认「=主 / 主=」+ 枚举值 + 来源=。
_RE_FRAG = re.compile(
    r"^\s*((?:[\[【]\s*自\s*检\s*[\]】]\s*)?(?:主\s*)?[=＝]\s*"
    r"(?:接住|判断|事实|执行|轻接|接梗|看)"
    r"[^\n]{0,80}?来源\s*=\s*" + _SRC + r")\s*", re.S)


def _clean(body: str) -> str:
    return _RE_DRAFT.sub("", (body or "").lstrip())


def parse(text: str) -> tuple[str, str]:
    """返回 (标签, 正文)。没有标签时标签为空串、正文原样。"""
    t = text or ""
    m = _RE_OPEN.match(t)
    if m:
        t = "[自检]" + t[m.end():]
    for rx in (_RE_FULL, _RE_LINE, _RE_FRAG):
        m = rx.match(t)
        if m:
            return m.group(1).strip(), _clean(t[m.end():])
    # 整条回复只有自检行、没有正文：模型偶尔这么写。当正文发出去，
    # 用户会看到一行 [自检]；这里判空，让上层走兜底。
    if _RE_OPEN.match(t) and len(t.strip()) <= 140 \
            and not re.search(r"[。！？!?]", t):
        return t.strip()[4:].strip(), ""
    return "", t


def looks_like_tag(text: str) -> bool:
    """流式判断用：开头像自检标签（可能还没写完）。

    流式第一个分片可能只有「[」或「[自」，原来的 startswith("[自检]")
    会判 False，于是把标签当正文切句发出去（2026-11-13 下午那次整行 [自检]）。
    """
    t = (text or "").lstrip()
    if not t:
        return False
    for tag in ("[自检]", "【自检】"):
        if t.startswith(tag) or tag.startswith(t):
            return True
    # 残片形态：模型漏写「[自检] 主」，直接以「=接住」「主=接住」开头。
    # 不认这个的话，流式第一片就判成正文，后面再怎么 parse 也晚了。
    if re.match(r"^(?:主\s*)?[=＝]\s*(?:接|判|事|执|轻|看)", t):
        return True
    return False


# 模型偶尔把「怎么组织这条回复」的内心独白当正文发出来。2026-11-12 实测：
# 「自检那行先按要求写？系统要求格式——需写一行自检。但注意「标签不会发给用户」。
#  我照写。正文：哪个想法，联合那套还是连 Claude 也不碰了短一点。…」
# 这类文本没有任何信息量，还会进历史。只认强特征、且至少命中两条，
# 避免误杀真正在讨论「正文」的回复（比如用户让它写文章）。
_META_MARKERS = ("自检那行", "系统要求格式", "需写一行自检", "标签不会发给用户",
                 "我照写", "正文：", "内心独白", "让我想想怎么")


def is_meta(text: str) -> bool:
    """整段是不是「怎么回复」的独白，而不是回复本身。"""
    s = (text or "").strip()
    if not s or len(s) > 600:      # 长文里的「正文：」多半是真的在写东西
        return False
    return sum(m in s for m in _META_MARKERS) >= 2
