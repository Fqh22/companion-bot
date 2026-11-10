"""[orchestration] 后台任务的启动入口：不依赖 NoneBot。

QQ 插件用 `start_background`；新通道用同一个，不必重写调度循环。
主动消息的循环（30 分钟 tick）还在 plugins/proactive 里，等有第二个通道时再抽。
"""
from __future__ import annotations


def start_background(core, send):
    """启动定时任务（日记 / 复盘 / 清理 / 周报）。

    send: async (text: str) -> bool，由通道提供。
    返回 Scheduler；调用方负责 stop()。
    """
    from app_core.scheduling.scheduler import Scheduler

    sched = Scheduler(core, send)
    sched.start()
    return sched
