"""命令清单：/help 的唯一真相源。

**为什么单独放一层**：命令分散在 6 个插件里，/help 的文字以前在 admin 里手写，
两边没有约束——实测注册了 36 条，help 只列了 6 条，自己做的东西自己都想不起来。
现在命令只写在这里，admin 只负责渲染；lint 会核对「注册了但没写进清单」，
以及「写进清单但根本没注册」。

**为什么分成日常/维护**：29 条命令全展开，/help 本身就成了说明书。
实际数据（2026-11-10，11 天日志）显示日常只用到 /usage、/help、/prompt_last 几条，
维护命令十几条从没被敲过。日常的留在 /help，其余收进 /debug——能力一个不少，
但看 /help 不用再扫一遍运维手册。
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
        ("diary_show", "看某天的日记：/diary_show 11-04"),
        ("remind_list", "看还没到期的提醒"),
        ("remind_cancel", "取消一条：/remind_cancel 序号"),
        ("note_list", "最近写了哪些笔记"),
        ("meme", "手动发一张：/meme 开心"),
        ("debug", "维护与排障命令（18 条）"),
    ]),
]

# 维护：低频，但坏了要靠它们。全部收在 /debug 下面。
DEBUG_GROUPS: list[tuple[str, list[tuple[str, str]]]] = [
    ("记忆", [
        ("mem_status", "记忆链路健康快照（条数 / 最近召回 / 今日矛盾判定）"),
        ("memory_cleanup", "手动跑一次分层归档"),
        ("memory_conflicts", "看被新事实推翻的旧记忆（判错了可恢复）"),
    ("forget", "忘掉一条记忆：/forget 关键词 → 编号确认"),
    ("trace", "最近几轮读了哪些记忆"),
        ("memory_restore", "从归档/被取代里捞回来：/memory_restore 关键词"),
    ]),
    ("情绪", [
        ("mood_history", "最近几天的情绪曲线：/mood_history 7"),
    ]),
    ("日记与复盘", [
        ("diary_list", "最近写了哪些日记"),
        ("diary_write", "补写今天的日记"),
        ("review_now", "立刻跑一次复盘（不等到 03:40）"),
        ("weekly_now", "立刻生成一份周报"),
    ]),
    ("知识库", [
        ("kb_reindex", "重建知识索引（改了笔记不想等整点）"),
        ("kb_verify", "人工确认某页还成立：/kb_verify 标题"),
        ("kb_deprecate", "把某页标成已废弃：/kb_deprecate 标题"),
        ("raw_archive", "把昨天的对话原文归档"),
    ]),
    ("系统", [
        ("prompt_last", "看最近一轮发给模型的 prompt（排障）"),
        ("meme_stats", "表情包使用统计"),
        ("profile_forget", "删一个画像字段：/profile_forget 职业与求职"),
        ("profile_clear", "清空整份画像（会从下次复盘重新长）"),
        ("reload_persona", "重载人格文件，不用重启"),
    ]),
]


def names() -> list[str]:
    """全部命令名（不带斜杠）。lint 用它核对注册表，日常 + 维护都要算。"""
    return [n for _, items in GROUPS + DEBUG_GROUPS for n, _ in items]


def _render(groups: list[tuple[str, list[tuple[str, str]]]]) -> str:
    lines: list[str] = []
    for title, items in groups:
        lines.append("【" + title + "】")
        for name, desc in items:
            lines.append("/" + name + " " + desc)
    return chr(10).join(lines)


def text() -> str:
    """日常清单。/debug 里那 18 条不展开，只留一句提示。"""
    tail = "其余维护命令：/debug（记忆 / 情绪 / 复盘 / 知识库 / 系统）"
    return chr(10).join([_render(GROUPS), tail, "全部为超管命令；群里不响应。"])


def debug_text() -> str:
    """/debug 的正文：维护命令全量。"""
    return chr(10).join([_render(DEBUG_GROUPS), "全部为超管命令；群里不响应。"])
