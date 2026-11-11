"""[domain] 输入语义：每条消息进来时先给它打标，派活时才能精准带上下文。

为什么需要：聊天里的一句话常常省略上下文；
「把那个 PPT 第三页改一下」里，「那个 PPT」到底是什么，需要单独解析。
所以在消息进来的那一刻就把语义抽出来（要动手做吗 / 说的是哪个文件 / 关键主题），
派活时按这个把文件和相关记忆一起送过去，而不是把整段聊天倒进去。

全部是规则，不调模型：这一步在每条消息的必经路径上，多一次调用就多一次延迟和费用。
规则判不准时退回「当聊天处理」——不派活最多是多聊一句，派错活才是真麻烦。
"""
from __future__ import annotations

import re
from datetime import datetime

# 要动手做的动作词。只列「会产生东西」的动词：
# 「看看」「说说」这类不算，否则每句闲聊都会被判成任务。
TASK_VERBS = (
    "改", "生成", "整理", "转换", "转成", "合并", "拆分", "提取", "统计",
    "写一个", "写个", "做个", "做一个", "帮我做", "帮我写", "帮我改",
    "跑一下", "跑个", "批量", "重命名", "替换", "翻译成", "排版", "修一下",
    "优化一下", "分析一下这个文件",
)

# 引用已有文件/事情的说法
REFER = ("那个", "这份", "这个文件", "刚才", "上次", "前面那个", "刚发")

# 文件名里**不含空格**：带空格会把「改一下 报告.docx」整段吞进去。
FILE_RE = re.compile(
    r"[\w\u4e00-\u9fa5\-（）()]{1,40}?\.(?:docx?|pptx?|xlsx?|pdf|csv|txt|md|png|jpe?g|zip)",
    re.I)


def mentioned_files(text: str) -> list[str]:
    """句子里点名提到的文件（只取文件名，路径由调用方补）。"""
    return [m.group(0).strip() for m in FILE_RE.finditer(text or "")]


def wants_task(text: str, has_pending_file: bool = False) -> bool:
    """这句话是不是「让我动手做点什么」。

    带文件的场合放宽：他刚发完文件，说「第二段改成…」就该派活，
    哪怕句子里没有动词（「第二段太小了，改大点」有动词，但「标题换黑体」没有）。
    """
    t = (text or "").strip()
    if not t:
        return False
    if any(v in t for v in TASK_VERBS):
        return True
    if has_pending_file and len(t) >= 4:
        # 刚发完文件又说了句话：默认就是在交代怎么改
        return True
    return False


def refers_to_previous(text: str) -> bool:
    t = (text or "").strip()
    return any(w in t for w in REFER)


# 分析类意图：疑问形式 + 分析词，**两条都中**才算。
# 只命中一条会误伤陈述句（「还在学增删改查呢」有「呢」，但不是要判断）；
# 语义相似度试过，分布太平（陈述句 0.55-0.62，真问题 0.51），不用。
_Q_FORM = ("？", "?", "吗", "呢", "怎么", "什么", "哪", "该不该", "行不行",
           "值不值", "靠不靠谱", "是不是")
_ANALYTIC = ("选", "该", "值", "靠", "对比", "分析", "形势", "趋势", "缺口",
             "风险", "问题", "方向", "方案", "情况", "区别", "行不行")


def looks_analytical(text: str) -> bool:
    """这句是不是在「要判断 / 要方向」，而不是陈述或闲聊。"""
    t = text or ""
    return any(k in t for k in _Q_FORM) and any(k in t for k in _ANALYTIC)


def keywords(text: str, limit: int = 6) -> list[str]:
    """粗提主题词，用来做「相关记忆」的检索 query。

    不做分词：中文分词的依赖和不确定性不值得，直接把句子里的实词片段
    （2 字以上的连续汉字/英文）按长度排，取最长的几个当检索词。
    """
    t = (text or "").strip()
    segs = re.findall(r"[\u4e00-\u9fa5]{2,}|[A-Za-z][A-Za-z0-9_]{2,}", t)
    stop = {"帮我", "这个", "那个", "一下", "可以", "什么", "怎么", "然后", "现在"}
    segs = [s for s in segs if s not in stop]
    segs.sort(key=len, reverse=True)
    out: list[str] = []
    for s in segs:
        if s not in out:
            out.append(s)
        if len(out) >= limit:
            break
    return out


def analyze(text: str, has_pending_file: bool = False) -> dict:
    """一条消息的语义画像。字段都是可选的，缺省就是「没提到」。"""
    t = (text or "").strip()
    return {
        "text": t,
        "task": wants_task(t, has_pending_file),
        "files": mentioned_files(t),
        "refers": refers_to_previous(t),
        "terms": keywords(t),
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
