"""[orchestration] 插件门面：插件只能通过 `core.<ns>.<func>` 调业务。

为什么不用「给 ChatCore 加 20 个方法」：B2 刚把入口类从 53 个方法降到 19，
再堆一层门面方法就回到原点。命名空间对象只暴露插件实际用到的模块，
`CORE_API` 按 `core.<ns>.<func>` 两层登记，`ops/lint-plugins.py` 逐条检查——
私有成员（`core.learn._touch_wiki`）直接 FAIL。

插件写法：
    from plugins.chat import core
    await core.learn.review(core)
    core.audit.recent(5)

责任边界：这里只做「命名空间 → 模块」的绑定，不包装、不改语义；
业务函数自己的契约由各自的测试保证。
"""
from __future__ import annotations


def build() -> dict[str, object]:
    """返回 {命名空间: 模块}。ChatCore.__init__ 把它们挂成实例属性。"""
    from app_core import (audit, diary, introspect, kb, learn, meme, mood,
                          proactive_channels, profile, relational, remind,
                          scheduler, tools, wiki)
    from app_core import help as help_mod
    from app_core import proactive_runtime

    return {
        "learn": learn,
        "audit": audit,
        "kb": kb,
        "wiki": wiki,
        "diary": diary,
        "meme": meme,
        "mood": mood,
        "remind": remind,
        "profile": profile,
        "help": help_mod,
        "introspect": introspect,
        "tools": tools,
        "scheduler": scheduler,
        "proactive_channels": proactive_channels,
        "proactive_runtime": proactive_runtime,
        "relational": relational,
    }
