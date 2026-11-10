"""任务意图：只在规则明确时把聊天升级成 agent 任务。

不确定就返回 False，让 ChatCore 正常聊天；误判的代价比多问一句高。
"""
from __future__ import annotations

import re
from pathlib import Path

_EXPLICIT = re.compile(r"^(?:交给|让)\s*agent\s*[:：]?\s*(?:做|干)?\s*", re.I)
_TASK_VERBS = (
    "帮我做", "帮我改", "帮我生成", "帮我整理", "帮我导出", "帮我重排",
    "把这份", "把这个文件", "把收到", "把文件", "整理成", "改成一版",
    "改成", "重排", "转成", "做成", "生成一个", "提取", "合并", "批量",
    "查一下然后", "按这个改", "按这份改",
)
_FILE_CUES = ("这份", "这个文件", "收到", "附件", "文件", "pdf", "docx",
              "xlsx", "pptx", "简历", "表格")
# 这些词只有“手里有文件”时才算任务；单独说“调整一下心态”不抢聊天
_FILE_VERBS = ("调整", "修改", "改动", "改一下", "改下", "更新", "改到",
               "改掉", "改过来", "替换", "删除", "加上", "补上")
_START = ("开始", "做", "做吧", "好", "可以", "行", "直接做", "开始吧")
_CANCEL = ("不用", "算了", "取消", "先不", "别做", "不做了", "继续聊")


def is_explicit(text: str) -> bool:
    return bool(_EXPLICIT.match((text or "").strip()))


def explicit_goal(text: str) -> str:
    return _EXPLICIT.sub("", (text or "").strip()).strip()


def is_task_request(text: str, has_file: bool = False) -> bool:
    t = (text or "").strip()
    if not t or t.startswith("/"):
        return False
    if is_explicit(t):
        return True
    # 纯问句/感叹句不抢：只有带文件或明确动作词才升级
    if t.endswith(("吗", "么", "呢")) and not has_file:
        return False
    if any(v in t for v in _TASK_VERBS):
        return True
    if has_file and any(v in t for v in _FILE_VERBS):
        return True
    if has_file and any(c in t.lower() for c in _FILE_CUES):
        return True
    return False


def is_start(text: str) -> bool:
    return (text or "").strip() in _START


def is_cancel(text: str) -> bool:
    return (text or "").strip() in _CANCEL


def file_names(paths: list[str] | None) -> list[str]:
    return [Path(str(p)).name for p in (paths or [])]


def confirm_text(goal: str, files: list[str] | None) -> str:
    names = file_names(files)
    f = "、".join(names) if names else "无（需要文件的话，先发文件再继续）"
    return ("要交给 agent 做吗？\n"
            f"目标：{goal[:120]}\n"
            f"附件：{f}\n"
            "回「开始」；回「不用」继续聊。")
