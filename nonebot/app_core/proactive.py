"""主动消息的判断层：该不该开口、说什么话题。

为什么单独一层：这段规则决定了它「像不像人」——太勤像骚扰，太懒像失联，
是要反复调的。混在插件里既没法离线测（导入就要 nonebot），也看不清每条规则的先后。
这里只做判断，不碰数据库、不发消息、不调模型。
"""
from __future__ import annotations

HOUR_START, HOUR_END = 9, 23   # 只在这段时间开口


def period(hour: int) -> str:
    """给主动消息的提示词用：模型对「现在几点」没有直觉，得把时段写给它。"""
    if 5 <= hour < 9:
        return "早上"
    if 9 <= hour < 12:
        return "上午"
    if 12 <= hour < 14:
        return "中午"
    if 14 <= hour < 18:
        return "下午"
    if 18 <= hour < 23:
        return "晚上"
    return "深夜"
MIN_GAP = 7200                 # 两次之间至少 2 小时
QUIET_AFTER_USER = 1200        # 刚聊过 20 分钟不打扰
DAILY_MAX = 4
TOPIC_COOLDOWN_DAYS = 7        # 同一件事一周内不重复提


def daily_cap(reply_rate: float | None) -> int:
    """按最近 7 天的回应率定今天的上限。宁少勿多：烦人一次要好几条正常回复才修得回来。"""
    if reply_rate is None:
        return DAILY_MAX
    if reply_rate < 0.2:
        return 1
    if reply_rate < 0.4:
        return 2
    return DAILY_MAX


def gate(hour: int, since_sent: float, since_user: float,
         sent_today: int, cap: int, mood: dict | None,
         rolls: list[float], threads: int = 0) -> tuple[bool, str]:
    """返回 (该不该开口, 原因)。

    顺序有讲究：先看「能不能说」（时段/频率），再看「有没有话头」。
    原因只用于日志——「今天为什么没说话」应该能一眼查到。

    2026-11-10 按真实使用收窄：11 天发了 5 条主动消息，curious 3 条、memory 1 条
    全都没得到回复，唯一被接住的是 followup（「那件事进度咋样了」）。
    判据因此从「抽签 + 泛话题」改成「他留了话头才开口」——没话找话对这个人无效，
    他画像里明写着讨厌空话。rolls 保留只为签名兼容；心情只决定怎么说，不决定说不说。
    """
    if not HOUR_START <= hour < HOUR_END:
        return False, "不在时段"
    if since_sent < MIN_GAP:
        return False, "距上次太近"
    if since_user < QUIET_AFTER_USER:
        return False, "刚聊过"
    if sent_today >= cap:
        return False, "今日已满"
    if threads and since_user >= 2 * 3600:
        return True, "他留的话头还悬着"
    return False, "没有话头，不没话找话"


def pick_topic(followups: list[tuple], curious: list[tuple], memories: list[tuple],
               recent: set[str], branch_roll: float,
               pick_roll: float) -> tuple[str, str, str] | None:
    """从三类候选里挑一个 (key, topic, why)。

    优先级：他之前说要做的事（50%）→ 还没搞懂的问题（30%）→ 重要但很久没想起的事。
    一周内提过的 key 直接过滤，避免连着两天说同一件事。

    三个列表都形如 [(key, topic, why)]，由调用方从各自的存储里取。
    """
    def fresh(pool):
        return [x for x in pool if x[0] not in recent]

    fu, cu, me = fresh(followups), fresh(curious), fresh(memories)
    if fu and branch_roll < 0.5:
        pool = fu
    elif cu and branch_roll < 0.8:
        pool = cu
    elif me:
        pool = me
    elif fu:
        pool = fu
    else:
        return None
    idx = min(int(pick_roll * len(pool)), len(pool) - 1)
    key, topic, why = pool[idx]
    return key, topic, why
