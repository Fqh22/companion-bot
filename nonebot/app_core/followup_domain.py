"""[domain] followup 的纯状态机：信号、准入、解析结果，不碰数据库。"""
from __future__ import annotations

import re

MAX_TEXT = 160
NEAR_DUP = 0.62
COOLDOWN_DAYS = 5
MAX_AGE_DAYS = 60
MAX_ASKS = 6
KEEP_MAX_OPEN = 30
ASK_MAX_AGE_DAYS = 14
ASK_MAX_TIMES = 3

_BOT_TASK = re.compile(
    r"^\s*(我|我们)(需要|要|得|该|会|先|去|来|帮|给)|^告诉他|^把他|^帮他|^给他|"
    r"^回答|^补讲|^先给|^以后我|^下次我|^再帮|^确认|^确定")
_META_UNANSWERED = re.compile(
    r"还没(回复|答|说|确认|讲|发|回)|没回复|没回答|没答|未回复|还没告诉我|没告诉我")
_UNCLEAR = re.compile(r"是否.*(还不清楚|不清楚|没确认|未确认)")
_RULE = re.compile(r"^(以后|每次|下次)|以后他让我|先搜索对比再给结论")
_ONE_OFF = re.compile(
    r"^(当天|今天|明天|后天|周[一二三四五六日]).{0,20}"
    r"(面试|考试|会议|体检|报到|开庭|航班|车次|复查)|^面试前")

STATUSES = ("todo", "doing", "partial", "confirming", "done", "cancelled", "postponed")
CLOSED = ("done", "cancelled")

DONE_SIGNALS = ("完成", "完了", "好了", "交了", "弄完", "搞定", "办完",
                "结束", "做完", "搞完", "交上", "发出去了", "关掉了",
                "答过", "答完了", "做过了", "弄过了", "看过了", "处理过了")
PARTIAL_SIGNALS = ("做了一半", "弄了一半", "一半", "刚开始", "还在弄",
                   "在弄", "推进中", "有点进展", "做了一部分")
POSTPONE_SIGNALS = ("延期", "推迟", "往后推", "改天再", "下周再", "下个月再",
                    "暂时不", "先不弄", "缓一缓")
DROP_SIGNALS = ("忘了", "忘记", "不记得", "算了", "不弄了", "不想弄",
                "先放着", "再说吧", "不用了", "不搞了")
NEGATIONS = ("没", "未", "别", "尚未", "不")
AFFIRM = ("对", "是", "嗯", "没错", "是的", "对啊", "对的", "确实", "办完了", "是的呀")


def askable(text: str) -> bool:
    """这条待办值不值得主动问。"""
    t = (text or "").strip()
    if not t or len(t) > 120:
        return False
    if _BOT_TASK.search(t) or _META_UNANSWERED.search(t) or _UNCLEAR.search(t):
        return False
    if _RULE.search(t) or _ONE_OFF.search(t):
        return False
    return True


def _overlap(a: str, b: str) -> float:
    sa = set(a or "")
    if not sa:
        return 0.0
    return len(sa & set(b or "")) / len(sa)


def _signal_hit(text: str, signals: tuple[str, ...]) -> bool:
    for s in signals:
        start = 0
        while True:
            i = text.find(s, start)
            if i < 0:
                break
            window = text[max(0, i - 5):i]
            if not any(n in window for n in NEGATIONS):
                return True
            start = i + len(s)
    return False


def resolve_from(text: str, cands: list[tuple[int, str]],
                 last_asked: dict | None = None) -> dict:
    """纯解析：候选/最近问过由调用方查好传进来。"""
    t = (text or "").strip()
    out: dict = {"done": [], "confirm": [], "status": {}, "drop": []}
    if len(t) < 2:
        return out
    forced = False
    if not cands:
        la = last_asked
        if la and (_signal_hit(t, DONE_SIGNALS) or _signal_hit(t, PARTIAL_SIGNALS)
                   or _signal_hit(t, POSTPONE_SIGNALS) or _signal_hit(t, DROP_SIGNALS)
                   or any(w in t for w in ("还没", "没呢", "没有", "忘了"))):
            cands = [(la["id"], la["text"])]
            forced = True
    if not cands:
        return out
    if _signal_hit(t, PARTIAL_SIGNALS):
        for fid, _ in cands:
            out["status"][fid] = "partial"
        return out
    if _signal_hit(t, POSTPONE_SIGNALS):
        for fid, _ in cands:
            out["status"][fid] = "postponed"
        return out
    if _signal_hit(t, DROP_SIGNALS):
        for fid, _ in cands:
            out["drop"].append(fid)
        return out
    if not _signal_hit(t, DONE_SIGNALS):
        return out
    unsure = any(w in t for w in ("好像", "应该", "大概", "可能", "差不多"))
    for fid, ftxt in cands:
        ov = 1.0 if forced else _overlap(ftxt, t)
        if ov >= 0.85 and not unsure:
            out["done"].append(fid)
        else:
            out["confirm"].append(fid)
    return out
