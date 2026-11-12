"""命令清单：/help 的唯一真相源。

**为什么单独放一层**：命令分散在 6 个插件里，/help 的文字以前在 admin 里手写，
两边没有约束——实测注册了 36 条，help 只列了 6 条，自己做的东西自己都想不起来。

**为什么只剩 12 条**：2026-11-12 实测，维护命令从没被敲过。清单只留日常和排障必需的；
其余命令**代码还在**（能用），但不再出现在 /help 和 /debug 里——能力不删，表面积减半。
它们登记在 `HIDDEN` 里，lint 用它区分「没写」和「故意不展示」。
"""
from __future__ import annotations

# 日常：会天天用到的。顺序按使用频率排。
GROUPS: list[tuple[str, list[tuple[str, str]]]] = [
    ("日常", [
        ("ping", "测试 + 配置状态（含配置改了没重启）"),
        ("help", "这份清单"),
        ("usage", "今日 / 近 7 天 token 与费用"),
        ("mood", "当前心情 / 精力 / 互动热度"),
        ("profile", "看它记住的你（置信度、多久没确认、上次是什么）"),
        ("debug", "维护与排障命令"),
    ]),
]

# 维护：低频，但坏了要靠它们。
DEBUG_GROUPS: list[tuple[str, list[tuple[str, str]]]] = [
    ("记忆", [
        ("mem_status", "记忆链路健康快照（条数 / 最近召回 / 今日矛盾判定）"),
        ("forget", "忘掉一条记忆：/forget 关键词 → 编号确认"),
        ("trace", "最近几轮读了哪些记忆"),
        ("memory_restore", "从归档/被取代里捞回来：/memory_restore 关键词"),
    ]),
    ("系统", [
        ("prompt_last", "看最近一轮发给模型的 prompt（排障）"),
        ("reload_persona", "重载人格文件，不用重启"),
    ]),
]

# 注册了但不再展示：代码与 handler 都还在，只是不进 /help、不进 /debug。
# lint 看到它们在注册表里，会跳过「没写进清单」的检查。
HIDDEN = frozenset({
    # 日常里撤下来的
    "diary_show", "remind_list", "remind_cancel", "note_list", "meme",
    # 记忆 / 情绪
    "memory_cleanup", "memory_conflicts", "mood_history",
    # 日记与复盘（有定时任务代替）
    "diary_list", "diary_write", "review_now", "weekly_now",
    # 知识库
    "kb_reindex", "kb_verify", "kb_deprecate", "raw_archive",
    # 系统
    "meme_stats", "profile_forget", "profile_clear",
})


def names() -> list[str]:
    """展示中的命令名（不带斜杠）。"""
    return [n for _, items in GROUPS + DEBUG_GROUPS for n, _ in items]


def hidden() -> frozenset:
    """注册但不展示的命令。"""
    return HIDDEN


def _render(groups: list[tuple[str, list[tuple[str, str]]]]) -> str:
    lines: list[str] = []
    for title, items in groups:
        lines.append("【" + title + "】")
        for name, desc in items:
            lines.append("/" + name + " " + desc)
    return chr(10).join(lines)


def text() -> str:
    return chr(10).join([_render(GROUPS), "全部为超管命令；群里不响应。"])


def debug_text() -> str:
    return chr(10).join([_render(DEBUG_GROUPS), "全部为超管命令；群里不响应。"])


def all_names() -> list[str]:
    """展示 + 隐藏，给需要完整清单的地方用（比如测试）。"""
    return names() + sorted(HIDDEN)
