"""情绪状态：心情 / 精力 / 互动热度。

三个量都有明确的时间尺度和物理意义，而不是三个计数器：

| 量 | 含义 | 时间常数 |
|---|---|---|
| `valence` | 此刻心情相对基线的位置 | 快：τ=5h 回基线 |
| `valence_base` | 这一阵的心情基线（天级记忆） | 慢：τ=72h 追随 valence |
| `energy` | 还能聊多久的余量 | 按节奏消耗，睡眠恢复 |
| `social` | 最近的互动密度 | τ=6h 的 EMA |

三条硬约束（2026-11-01 重写的原因）：

1. **撞界自动减速**：增量乘以 `1-|valence|`，越极端越难推动。
   原来是线性加 `±0.06/0.10` 到 ±1 硬截断——从 +0.9 吐槽两下就掉到 0.7，
   从 -0.9 吐槽五条还是 -1.0，同一句话在不同状态上效果一样，那是计数器。
2. **衰减必须可复合**：用 `1-e^{-h/τ}`。原来的 `min(0.9, 0.12h)` 在 7.5 小时后
   不再增长，8h、24h、72h 静默残留都是 10%，等于「忘到一成就不忘了」；
   而且 settle 一次和分三次结果不同——决定情绪的是消息条数，不是时间。
3. **基线不是 0**：单次波动回到 `valence_base`，基线的缓慢移动才是
   「这一周过得怎么样」。原来固定回 0，情绪只有秒级抖动，没有天级状态。
"""
from __future__ import annotations
from __future__ import annotations

from app_core import paths as _paths

from app_core.mood_domain import (  # noqa: F401
    CLAMP, CORRECTION_HINTS, DEFAULT, ENERGY_BASE_COST, ENERGY_BURST_COST,
    ENERGY_FLOOR, ENERGY_REST, EVENTS, NEGATIVE, NIGHT_DRAIN, POSITIVE,
    SLEEP_RECOVER_TAU, SOCIAL_REST, TAU_BASE, TAU_ENERGY, TAU_SOCIAL,
    TAU_VALENCE, _bump, _clamp, _cos, _hours_between, _note, _pack_vec,
    _unpack_vec, classify_progress, describe, observe, observe_event,
    settle, task_cost,
)
from app_core.mood_store import (  # noqa: F401
    apply_progress, apply_task, load, recent, save, snapshot, summary,
)

STATE_DIR = _paths.DATA / "plugin_data/mood"
STATE = STATE_DIR / "state.json"
CHAT_DB = _paths.DATA / "chat.db"
