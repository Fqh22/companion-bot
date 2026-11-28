"""可独立运行的离线回归用例。"""
from offline_regression.fixtures import (
    chat_mod, check,
)


async def run():
    # ---------- Z16. 发送节奏：长回答要自动加速 ----------
    from app_core import pacing as pacing_mod

    slow = [pacing_mod.gap_seconds(i) for i in range(5)]
    fast = [pacing_mod.gap_seconds(i) for i in range(9, 20)]
    check("短回答保持原节奏", all(1.2 <= x <= 2.8 for x in slow), str([round(x, 2) for x in slow]))
    check("段数一多自动加速", all(x <= 1.0 for x in fast), str([round(x, 2) for x in fast]))
    check("已知总段数多也加速", pacing_mod.gap_seconds(0, 20) <= 1.0,
          str(round(pacing_mod.gap_seconds(0, 20), 2)))
    check("is_fast 判定", pacing_mod.is_fast(8) and not pacing_mod.is_fast(3))

    p_slow = pacing_mod.pace_seconds("一句话", 15, {"energy": 0.7})
    p_fast = pacing_mod.pace_seconds("一句话", 15, {"energy": 0.7}, fast=True)
    check("加速时打字停顿有上限", p_fast <= pacing_mod.FAST_PACE_CAP + 1e-9,
          f"{p_slow:.2f} → {p_fast:.2f}")
    check("夜里更慢", pacing_mod.pace_seconds("一句话", 2, {"energy": 0.7})
          > pacing_mod.pace_seconds("一句话", 15, {"energy": 0.7}))
    check("节奏有下限", pacing_mod.pace_seconds("", 15, {"energy": 0.1}) >= 0.15)

    # 回复预算：图片轮和工具轮都按 1600 的下限
    check("回复输出预算下限 1600", chat_mod.REPLY_MAX_TOKENS == 1600,
          str(chat_mod.REPLY_MAX_TOKENS))
