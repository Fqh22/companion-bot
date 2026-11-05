"""命令清单：/help 的唯一真相源。

**为什么单独放一层**：命令分散在 7 个插件里，/help 的文字却在 admin 里手写，
两边没有约束——实测注册了 36 条，help 只列了 6 条，自己做的东西自己都想不起来。
现在命令只写在这里，admin 只负责渲染；lint 会核对「注册了但没写进清单」，
以及「写进清单但根本没注册」。

排序按「多常用」而不是按插件：最常敲的放最上面。
"""
from __future__ import annotations

GROUPS: list[tuple[str, list[tuple[str, str]]]] = [
    ("日常", [
        ("ping", "测试 + 配置状态（含配置改了没重启）"),
        ("prompt_last", "看最近一轮发给模型的 prompt（排障）"),
        ("help", "这份清单"),
        ("profile", "看它记住的你（置信度、多久没确认、上次是什么）"),
        ("profile_forget", "删一个画像字段：/profile_forget 职业与求职"),
        ("profile_clear", "清空整份画像（会从下次复盘重新长）"),
        ("reload_persona", "重载人格文件，不用重启"),
    ]),
    ("情绪", [
        ("mood", "当前心情 / 精力 / 互动热度"),
        ("mood_history", "最近几天的情绪曲线：/mood_history 7"),
    ]),
    ("记忆", [
        ("memory_cleanup", "手动跑一次分层归档"),
        ("memory_conflicts", "看被新事实推翻的旧记忆（判错了可恢复）"),
        ("memory_restore", "从归档/被取代里捞回来：/memory_restore 关键词"),
        ("usage", "今日 / 近 7 天 token 与费用"),
    ]),
    ("日记与复盘", [
        ("diary_show", "看某天的日记：/diary_show 11-04"),
        ("diary_write", "补写今天的日记"),
        ("diary_list", "最近写了哪些日记"),
        ("review_now", "立刻跑一次复盘（不等到 03:40）"),
        ("weekly_now", "立刻生成一份周报"),
    ]),
    ("提醒", [
        ("remind_list", "看还没到期的提醒"),
        ("remind_cancel", "取消一条：/remind_cancel 序号"),
    ]),
    ("知识库", [
        ("note_list", "最近写了哪些笔记"),
        ("kb_reindex", "重建知识索引（改了笔记不想等整点）"),
        ("kb_verify", "人工确认某页还成立：/kb_verify 标题"),
        ("kb_deprecate", "把某页标成已废弃：/kb_deprecate 标题"),
        ("raw_archive", "把昨天的对话原文归档"),
    ]),
    ("技能", [
        ("skill_list", "它会哪些流程"),
    ]),
    ("表情", [
        ("meme", "手动发一张：/meme 开心"),
        ("meme_stats", "表情包使用统计"),
    ]),
    ("agentd（办事）", [
        ("task", "派活：/task 在当前目录生成平方表"),
        ("tasks", "最近的任务和状态"),
        ("progress", "看某个任务：/progress 12"),
        ("answer", "回答卡住的任务：/answer 12 用 xlsx"),
        ("approve", "批准危险命令：/approve 12"),
        ("cancel", "取消任务：/cancel 12"),
        ("pause", "暂停任务：/pause 12"),
        ("resume", "继续任务：/resume 12"),
        ("agent_usage", "agentd 今天的 token（/usage 是聊天那条）"),
    ]),
]


def names() -> list[str]:
    """全部命令名（不带斜杠）。lint 用它核对注册表。"""
    return [n for _, items in GROUPS for n, _ in items]


def text() -> str:
    """/help 的正文。太长就按组折叠成一行一条，不换行展开。"""
    lines: list[str] = []
    for title, items in GROUPS:
        lines.append("【" + title + "】")
        for name, desc in items:
            lines.append("/" + name + " " + desc)
    lines.append("全部为超管命令；群里不响应。")
    return chr(10).join(lines)
