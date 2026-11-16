"""[domain] 发送节奏：段与段之间停多久、每条发出前“打字”停多久。

为什么单独抽出来：这两个数直接决定一条长回答要发多久。实测有一次 422 字的回答
切成 **31 段**，按原来的固定节奏（段间 1.2–2.8s + 打字 0.15–2.5s）光发送就要 80 秒，
直接撞穿 `reply_deadline=45s` 的硬线——撞上以后用户只收到半句话，而且因为已经
发过几段，连“有点卡”都不会提示。

规则：**段数一多就自动加速**。短回答保持原来的“人味”，长回答不拖。
"""
from __future__ import annotations

import random
import re

SLOW_GAP = (1.2, 2.8)      # 普通节奏：段与段之间
FAST_GAP = (0.4, 1.0)      # 长回答：段数超过 FAST_AFTER 之后
FAST_AFTER = 8             # 超过这么多段就加速
FAST_PACE_CAP = 0.8        # 加速时“打字”停顿的上限（秒）
PACE_MAX = 2.5

VOICE_CHARS = 300          # 单条语音约 30 秒，与 tts.MAX_CHARS 对齐
VOICE_MAX_PARTS = 8        # 兜底：再长也别一口气发十几条语音

_SENT_END = re.compile(r"(?<=[。！？!?…\n])")


def split_for_voice(text: str, limit: int = VOICE_CHARS,
                    max_parts: int = VOICE_MAX_PARTS) -> list[str]:
    """把长文案按句切成多段，每段 ≤ `limit`；短文本原样返回一条。

    为什么在 domain 层而不是 tts.py：这是「怎么说」的节奏规则，不是合成实现；
    纯函数也方便护栏直接测。以前只有 `tts._clip_text`：超过 300 字直接把
    后半截丢掉，朗读长文案会莫名其妙停在半句上（CHANGELOG 11-16 已知限制）。
    切在句子边界，避免把词截断；单段路径与旧行为逐字节一致。
    """
    t = (text or "").strip()
    if not t:
        return []
    if len(t) <= limit:
        return [t]
    parts: list[str] = []
    buf = ""
    for sentence in _SENT_END.split(t):
        s = sentence.strip()
        if not s:
            continue
        # 超长单句：只能硬切，但切完仍按段发，不丢内容
        while len(s) > limit and len(parts) < max_parts:
            if buf:
                parts.append(buf)
                buf = ""
            parts.append(s[:limit])
            s = s[limit:]
        if len(parts) >= max_parts:
            break
        if not s:
            continue
        if buf and len(buf) + len(s) > limit:
            parts.append(buf)
            buf = s
        else:
            buf += s
    if buf and len(parts) < max_parts:
        parts.append(buf)
    return [p for p in parts[:max_parts] if p.strip()]


def gap_seconds(sent: int, total: int | None = None) -> float:
    """第 sent 段发完之后，等多久再发下一段。

    sent 从 0 开始；total 是已知总段数（流式时不知道，就只看 sent）。
    """
    try:
        n = int(sent)
    except Exception:
        n = 0
    fast = (total is not None and int(total) > FAST_AFTER) or n >= FAST_AFTER
    lo, hi = FAST_GAP if fast else SLOW_GAP
    return random.uniform(lo, hi)


def pace_seconds(text: str, hour: int, mood: dict | None = None,
                 fast: bool = False) -> float:
    """发出这一条之前的“打字”停顿。`fast=True` 时压低上限。"""
    m = mood or {}
    base = 0.35 + min(1.2, len(text or "") * 0.02)
    if hour >= 23 or hour < 7:
        base += 0.45
    try:
        e = float(m.get("energy", 0.7))
        v = float(m.get("valence", 0.0))
        s = float(m.get("social", 0.5))
        if e < 0.35:
            base += 0.5
        elif e > 0.8:
            base -= 0.1
        if v < -0.3:
            base += 0.25
        elif v > 0.4 and s > 0.6:
            base -= 0.1
        if s > 0.75:
            base -= 0.15
    except Exception:
        pass
    if fast:
        base = min(base, FAST_PACE_CAP)
    return max(0.15, min(PACE_MAX, base))


def is_fast(sent: int, total: int | None = None) -> bool:
    return (total is not None and int(total) > FAST_AFTER) or int(sent) >= FAST_AFTER
