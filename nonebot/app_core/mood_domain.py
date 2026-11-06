"""mood 的纯规则：不碰文件、不碰 sqlite。"""
from __future__ import annotations

import base64
import math
import struct
import time
from datetime import datetime, timedelta

from app_core import config

ENERGY_FLOOR = 0.05

POSITIVE = ("谢谢", "厉害", "不错", "喜欢", "开心", "哈哈", "牛", "棒", "好玩", "可爱", "辛苦了", "乖")
# 负面词表补齐了口语高频词：老表只有「烦/累/无语」这类书面词，
# 而人吐槽时说的是「离谱」「服了」「麻了」。词表缺词 = 情绪读不出来，
# 表现就是「想吐槽时它不发表情、接话也不冷」。
# 两点刻意为之：
#   * 不收「累死」「烦死」「心累」——它们含「累」「烦」，会被重复计一次，等于偷偷加倍权重；
#   * 不收「卧槽」「崩溃」——前者多是惊讶（正负都有），后者聊技术时会误伤。
NEGATIVE = ("烦", "累", "滚", "笨", "无语", "气死", "垃圾", "闭嘴", "讨厌", "蠢", "别烦",
            "离谱", "服了", "麻了", "郁闷", "气人", "受不了", "恶心")
# 他在纠正我：这类信号比负面词更值得记，说明「我刚才错了」
CORRECTION_HINTS = ("不对", "不是这样", "记错了", "说错了", "搞错", "你错了", "别瞎编",
                    "谁说的", "胡编", "有问题吧")

DEFAULT = {
    "valence": 0.1,
    "valence_base": 0.1,
    "energy": 0.7,
    "social": 0.5,
    "updated_at": 0.0,
    "last_user_ts": 0.0,
    # 当天计数：快照要用，也顺便告诉「今天聊了多少」
    "day": "",
    "msgs_today": 0,
    "events_today": 0,
    # 最近触发过的事，两三句就够。给 describe() 用：让语气有原因，不是凭空变脸。
    "why": [],
}

# ---------- 时间常数 ----------
TAU_VALENCE = 5.0        # 小时：单次情绪波动回到基线的尺度
TAU_ENERGY = 8.0         # 小时：白天闲下来时精力往静息值走
TAU_SOCIAL = 6.0         # 小时：互动热度的记忆长度
TAU_BASE = 72.0          # 小时：基线追随当前心情的尺度（「这一周」）
ENERGY_REST = 0.7
SOCIAL_REST = 0.2
NIGHT_DRAIN = 0.06       # 每小时：0-5 点的精力消耗
SLEEP_RECOVER_TAU = 2.0  # 小时：6-8 点按这个尺度向满电恢复
CLAMP = 1.0

# 单条消息的精力成本：基础 + 连发惩罚（间隔越短越贵）
ENERGY_BASE_COST = 0.004
ENERGY_BURST_COST = 0.016

# 事件对情绪的影响。回应超时、工具失败、被纠正——原来这些都不进情绪，
# 它只有「听到什么」，没有「经历了什么」。
EVENTS = {
    "timeout": {"valence": -0.03, "energy": -0.05, "why": "回复超时了"},
    "tool_fail": {"valence": -0.02, "energy": -0.03, "why": "工具没成功"},
    "user_correct": {"valence": -0.08, "why": "他纠正了我"},
    "reunion": {"valence": 0.02, "social": 0.15, "why": "隔了很久才说话"},
    "reply_ok": {"energy": -0.008},
}



PROGRESS_SAME = 0.82      # 以上：同一件事又问一遍
PROGRESS_FOLLOW = 0.55    # 以上：顺着往下问
PROGRESS_SWITCH = 0.45    # 以下：换话题（中间留死区，避免噪声）
PROGRESS_DELTA = {"advance": 0.03, "switch": 0.03, "repeat": -0.04}
PROGRESS_ENERGY = {"repeat": -0.02}


def _clamp(x: float, lo: float = -CLAMP, hi: float = CLAMP) -> float:
    return max(lo, min(hi, x))

def _pack_vec(vec: list[float]) -> str:
    try:
        return base64.b64encode(struct.pack(f"{len(vec)}f", *vec)).decode()
    except Exception:
        return ""

def _unpack_vec(blob: str) -> list[float]:
    try:
        raw = base64.b64decode(blob)
        return list(struct.unpack(f"{len(raw) // 4}f", raw))
    except Exception:
        return []

def _cos(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0

def classify_progress(cos: float) -> str:
    if cos >= PROGRESS_SAME:
        return "repeat"
    if cos >= PROGRESS_FOLLOW:
        return "advance"
    if cos < PROGRESS_SWITCH:
        return "switch"
    return ""            # 死区：不做判断

def task_cost(rounds: int, chars: int) -> float:
    """这一轮回复「有多费劲」：按工具轮数和回答长度算，不按消息条数。

    原来的成本是「每条消息固定扣一点」，于是问一句答一句和查三次资料再写长答案
    一样累。实际后者才费劲：多一轮工具 = 多一次完整 LLM 调用，长答案 = 更多输出。
    上限 0.03，避免一次超长回答直接把精力打穿。
    """
    try:
        r = max(0, int(rounds))
        n = max(0, int(chars))
    except Exception:
        return 0.0
    cost = 0.004 * min(3, r) + 0.002 * (n // 120)
    return min(0.03, cost)

def _cfg() -> dict:
    """读 config.yaml 的 mood 段。读不到就用模块里的默认常量。"""
    try:
        return config.load().get("mood") or {}
    except Exception:
        return {}

def _note(s: dict, text: str, sign: int = 0, now: float | None = None) -> None:
    if not text:
        return
    why = list(s.get("why") or [])
    why.append({"ts": now or time.time(), "text": text, "sign": sign})
    s["why"] = why[-3:]

def _hours_between(start: float, end: float) -> list[tuple[int, float]]:
    """把 [start, end] 切成「按小时」的小段，返回 [(几点, 小时数)]。

    精力要看时钟（夜里掉、早上恢复），不能只看总时长——所以得知道
    「这 8 小时里哪几个小时是夜里」。分段后逐段应用，结果与切分方式无关。
    """
    out: list[tuple[int, float]] = []
    t = start
    guard = 0
    while t < end and guard < 24 * 8:      # 一周以上按一周算，别真的走 1000 小时
        nxt = min(end, (int(t // 3600) + 1) * 3600)
        out.append((datetime.fromtimestamp(t).hour, (nxt - t) / 3600.0))
        t = nxt
        guard += 1
    return out

def settle(s: dict, now: float | None = None) -> dict:
    """按流逝的真实时间衰减。惰性调用，但结果与调用次数无关。"""
    now = now or time.time()
    last = float(s.get("updated_at") or 0)
    if not last:
        s["updated_at"] = now
        return s
    hours = (now - last) / 3600.0
    if hours <= 0:
        return s
    hours = min(hours, 24 * 7)

    c = _cfg()
    tau_v = float(c.get("tau_valence_hours", TAU_VALENCE))
    tau_b = float(c.get("tau_base_hours", TAU_BASE))
    tau_s = float(c.get("tau_social_hours", TAU_SOCIAL))
    tau_e = float(c.get("tau_energy_hours", TAU_ENERGY))
    drain = float(c.get("night_drain", NIGHT_DRAIN))
    recover_tau = float(c.get("sleep_recover_hours", SLEEP_RECOVER_TAU))

    # 1) 心情与基线是一对耦合的线性系统，解析解才可复合：
    #      dv/dt = (b - v)/τv      db/dt = (v - b)/τb
    #    令 d = v - b，则 d' = -d·(1/τv + 1/τb)，所以
    #      d(t) = d0·e^{-kt},  b(t) = b0 + d0/(τb·k)·(1 - e^{-kt}),  v = b + d
    #    顺带一个好性质：持续的偏离会有 1/(1+τb/τv) 的比例沉淀进基线，
    #    「这阵子一直不错」真的会改变基线，而不是每次都弹回同一个数。
    k = 1.0 / tau_v + 1.0 / tau_b
    d0 = float(s.get("valence", 0.0)) - float(s.get("valence_base", 0.0))
    decay = math.exp(-k * hours)
    b_new = float(s.get("valence_base", 0.0)) + d0 / (tau_b * k) * (1 - decay)
    s["valence_base"] = _clamp(b_new)
    s["valence"] = _clamp(b_new + d0 * decay)

    # 3) 互动热度回到静息值
    ks = 1 - math.exp(-hours / tau_s)
    s["social"] = max(0.0, min(1.0,
                               float(s.get("social", 0.5))
                               + (SOCIAL_REST - float(s.get("social", 0.5))) * ks))

    # 4) 精力：夜里掉、早上按指数往满电走、白天闲下来回静息
    e = float(s.get("energy", ENERGY_REST))
    for hour, h in _hours_between(last, now):
        if 0 <= hour < 6:
            e -= drain * h
        elif 6 <= hour < 9:
            e += (1.0 - e) * (1 - math.exp(-h / recover_tau))
        else:
            e += (ENERGY_REST - e) * (1 - math.exp(-h / tau_e))
    s["energy"] = max(ENERGY_FLOOR, min(1.0, e))
    s["updated_at"] = now
    return s

def _burst(gap: float) -> float:
    """连发惩罚系数：15 秒内 1.0，一分钟内 0.6，五分钟内 0.3，否则 0.1。"""
    if gap < 15:
        return 1.0
    if gap < 60:
        return 0.6
    if gap < 300:
        return 0.3
    return 0.1

def _roll_day(s: dict, now: float) -> None:
    """跨天就把当日计数清零。计数只用于快照和「今天聊了多少」，不参与动力学。"""
    today = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
    if s.get("day") != today:
        s["day"] = today
        s["msgs_today"] = 0
        s["events_today"] = 0

def observe(s: dict, text: str, now: float | None = None,
            vec: list[float] | None = None) -> dict:
    """用户说了一句话：更新热度、精力、心情。

    vec 是这句话的 embedding（调用方已经为了召回算过一次，直接复用即可）。
    给了就用来判断「他是在推进、重复、还是换话题」——这是不依赖情感词的主信号。
    """
    now = now or time.time()
    _roll_day(s, now)
    s["msgs_today"] = int(s.get("msgs_today") or 0) + 1
    gap = now - float(s.get("last_user_ts") or 0)

    # 隔了半天以上才说话：算一次「重逢」，热度抬起来、心情微升
    if gap > 12 * 3600:
        s["social"] = max(float(s.get("social", 0.5)), 0.55)
        _bump(s, "valence", EVENTS["reunion"]["valence"], now)
        _note(s, EVENTS["reunion"]["why"], +1, now)
    s["last_user_ts"] = now

    # 热度：饱和增长（向 1 靠近，不会像原来 13 条就封顶）
    s["social"] = min(1.0, float(s.get("social", 0.5))
                      + (1.0 - float(s.get("social", 0.5))) * 0.10)

    # 话题推进：同一轮会话里，当前这句和上一句像不像。
    # 他从不夸人，所以「换话题」就是最可靠的「上一个问题解决了」。
    if vec and 0 < gap < 2 * 3600:
        prev = _unpack_vec(str(s.get("last_vec") or ""))
        cos = _cos(vec, prev) if prev else 0.0
        kind = classify_progress(cos) if prev else ""     # 没有上一句就没得比
        if kind == "repeat":
            _bump(s, "valence", -0.08, now)
            _bump(s, "energy", -0.05, now)
            _note(s, "又在问同一件事（可能上次没答到）", -1, now)
        elif kind == "advance":
            _bump(s, "valence", 0.02, now)
            _bump(s, "energy", -0.02, now)
        elif kind == "switch":
            _bump(s, "valence", 0.05, now)
            _note(s, "换话题了（上一个问题大概解决了）", +1, now)
    if vec:
        s["last_vec"] = _pack_vec(vec)

    # 精力：按节奏消耗，连发更贵
    if 0 < gap < 12 * 3600:
        c = _cfg()
        cost = (float(c.get("energy_base_cost", ENERGY_BASE_COST))
                + float(c.get("energy_burst_cost", ENERGY_BURST_COST)) * _burst(gap))
        s["energy"] = max(ENERGY_FLOOR, float(s.get("energy", ENERGY_REST)) - cost)

    low = (text or "").lower()
    pos = sum(1 for w in POSITIVE if w in low)
    neg = sum(1 for w in NEGATIVE if w in low)
    impulse = 0.06 * pos - 0.10 * neg
    if impulse:
        _bump(s, "valence", impulse, now)
        _note(s, f"他说了「{(text or '')[:12]}」", 1 if impulse > 0 else -1, now)

    if any(k in low for k in CORRECTION_HINTS):
        _bump(s, "valence", EVENTS["user_correct"]["valence"], now)
        _note(s, EVENTS["user_correct"]["why"], -1, now)

    s["updated_at"] = now
    return s

def _bump(s: dict, key: str, delta: float, now: float | None = None) -> None:
    """施加一个冲量。valence 越极端，同样的冲量推动越小（撞界自动减速）。"""
    cur = float(s.get(key, 0.0))
    if key == "valence":
        room = max(0.15, 1.0 - abs(cur))    # 极端时仍留 15% 的推动力，不会完全冻住
        cur = _clamp(cur + delta * room)
    elif key == "energy":
        cur = max(ENERGY_FLOOR, min(1.0, cur + delta))
    else:
        cur = max(0.0, min(1.0, cur + delta))
    s[key] = cur

def observe_event(s: dict, kind: str, now: float | None = None) -> dict:
    """机器人自己经历的事：超时、工具失败、被纠正……"""
    eff = EVENTS.get(kind)
    if not eff:
        return s
    now = now or time.time()
    _roll_day(s, now)
    s["events_today"] = int(s.get("events_today") or 0) + 1
    if "valence" in eff:
        _bump(s, "valence", eff["valence"], now)
    if "energy" in eff:
        _bump(s, "energy", eff["energy"], now)
    if "social" in eff:
        _bump(s, "social", eff["social"], now)
    if eff.get("why"):
        _note(s, eff["why"], -1 if eff.get("valence", 0) < 0 else 1, now)
    s["updated_at"] = now
    return s

def describe(s: dict) -> str:
    """给 prompt 的一行状态。

    数值原样给出去（0.24 和 0.25 不该表现一样），方向词只定基调，
    再附一句「为什么」——否则模型只看到一个标签，语气变脸也变得莫名其妙。
    整段控制在 100 字内：它每轮都注入，且排在缓存前缀之后，是全价的。
    """
    v = float(s.get("valence", 0.0))
    e = float(s.get("energy", ENERGY_REST))
    soc = float(s.get("social", 0.5))
    base = float(s.get("valence_base", 0.0))

    if v >= 0.5:
        tone = "心情不错，可以多说一句、带点玩笑"
    elif v >= 0.2:
        tone = "心情偏暖，语气放松些"
    elif v > -0.2:
        tone = "心情平稳，正常聊"
    elif v > -0.5:
        tone = "心情偏低，话短一点、别追问"
    else:
        tone = "心情差，接一句就够，别热络"
    if e < 0.3:
        tone += "；精力低，能短则短"
    if soc < 0.3:
        tone += "；不主动找话"
    elif soc > 0.75 and v > 0.2:
        tone += "；正聊得起来，可以接话茬"

    trend = "这阵子大致相同" if abs(base - v) < 0.08 else (
        "比这阵子好一些" if v > base else "比这阵子差一些")
    why = ""
    seen: list[str] = []
    for x in (s.get("why") or [])[-3:]:
        txt = x.get("text")
        if txt and txt not in seen:      # 同一件事连说两次（比如连发两条「谢谢」）只留一句
            seen.append(txt)
    if seen:
        why = "；最近：" + "、".join(seen[-2:])
    return (f"[你此刻的状态] 心情{v:+.2f}｜精力{e:.2f}｜互动{soc:.2f}（{trend}）。"
            f"语气：{tone}。数值越靠两端越明显，但别说出来、别解释状态" + why)
