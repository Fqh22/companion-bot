"""[domain] 召回打分的纯函数：不碰数据库、不碰缓存、不碰日志。

从 memory.recall 里抽出来的目的：调排序时不用读整个 Memory 类，
测试也可以直接喂数组，不用造库。
"""
from __future__ import annotations


def rank(scores, importance=None, ctime=None, last=None, now: float = 0.0,
         tau_days: float = 30.0, time_weight: float = 0.3,
         importance_weight: float = 0.1, keep_important: float = 0.8,
         repeat_window_min: float = 30.0, repeat_penalty: float = 0.35):
    """返回调制后的分数数组，顺序与输入一致。

    最终分 = cos × (1 - w + w×衰减) × (1 + w_i×重要性)
    再乘「最近被反复召回」的降权。
    """
    import numpy as np

    final = scores
    if importance is not None and ctime is not None:
        age_days = np.maximum(0.0, (now - ctime) / 86400.0)
        rec = np.exp(-age_days / max(1.0, tau_days))
        rec = np.where(importance >= keep_important, 1.0, rec)
        final = scores * ((1.0 - time_weight + time_weight * rec)
                          * (1.0 + importance_weight * importance))
    if last is not None and len(last) == final.shape[0]:
        idle_min = (now - last) / 60.0
        fresh = np.clip((repeat_window_min - idle_min) / max(1.0, repeat_window_min), 0, 1)
        final = final * (1.0 - repeat_penalty * fresh)
    return final
