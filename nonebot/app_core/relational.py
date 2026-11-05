"""相处状态：把「我现在什么心情」换成「他此刻需要什么」。

**为什么要换**：`mood.describe()` 出来的是一行自指的状态——
「心情偏低，话短一点」「精力低，能短则短」。它描述的是**我**，
模型读到之后会先想"我该怎么表现"，再想"他在说什么"。
做久了就会滑向表现型人格：越来越像一个有性格的人，越来越不像一个在听人说话的人。

这个模块只做一件事：把同样的几个信号（情绪数值、隔了多久、有没有悬着的话头）
翻译成**他那边的情况 + 这一轮该做什么**。数值没变，视角换了。

**输出极短**（≤ 90 字），每轮都注入、全价计费，所以只说"值得说"的那一种。
返回空串表示这一轮没什么特别的，不加也行——沉默比一句套话好。
"""
from __future__ import annotations


def gap_label(hours: float) -> str:
    """多久没说话 → 人话。只粗略分档，不精确到分钟（模型也用不上）。"""
    if hours < 1:
        return "一会儿"
    if hours < 3:
        return "一两个小时"
    if hours < 12:
        return f"{int(hours)} 小时"
    if hours < 24:
        return "大半天"
    return f"{int(hours / 24)} 天"


def describe(mood: dict | None, gap_hours: float, open_threads: int = 0,
             thread_hint: str = "") -> str:
    """返回这一轮的「相处状态」。空串 = 没什么可说的。

    判断顺序就是重要程度：他留的话头 > 气氛 > 是不是还在聊 > 隔了多久。
    """
    m = mood or {}
    v = float(m.get("valence", 0.0))
    base = float(m.get("valence_base", 0.0))
    low = (v - base) < -0.25
    warm = (v - base) > 0.25

    # 1) 还在聊：优先级最高。他正说着话，这时候去讲「我心情如何」就是插嘴
    if gap_hours < 1:
        tail = "他这会儿话多，可以多接一句。" if warm else ""
        return "[现在的相处] 还在聊，接着上一句往下说，别总结也别复述。" + tail

    # 2) 有悬着的事：他上次是半截话，回来多半想接着讲
    if open_threads:
        topic = f"「{thread_hint}」" if thread_hint else "上次那件事"
        return (f"[现在的相处] 他隔了 {gap_label(gap_hours)}才说话，{topic}还没说透。"
                "先问那个，别开新话题。")

    # 3) 气氛：说「这几轮沉」而不是「我心情差」
    if low:
        return "[现在的相处] 这几轮气氛偏沉，他提不太起劲。先接住他那句，别给方案。"

    # 4) 隔了很久
    if gap_hours >= 12:
        return (f"[现在的相处] 他隔了 {gap_label(gap_hours)}才出现。"
                "别装作一直在等，自然接上就行。")

    return ""


# 外人版人格里写着「每日 24:00 到次日 8:00 不回复」。这条只对别人生效，
# 主人半夜找它不该被晾着。
QUIET_START, QUIET_END = 0, 8


def is_quiet_hour(hour: int) -> bool:
    """该不该对「别人」保持安静。"""
    return QUIET_START <= hour < QUIET_END
