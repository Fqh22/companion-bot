"""[domain] 内置模型工具的权限与执行策略。"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ToolPolicy:
    read_only: bool
    public: bool = False
    irreversible: bool = False


_READ = ToolPolicy(read_only=True)
_WRITE = ToolPolicy(read_only=False)
BUILTIN_POLICIES = {
    "note_write": _WRITE,
    "note_list": _READ,
    "web_search": ToolPolicy(read_only=True, public=True),
    "web_open": ToolPolicy(read_only=False, irreversible=True),  # 可能写入学习笔记
    "diary_read": _READ,
    "kb_read": _READ,
    "kb_search": _READ,
    "memory_search": _READ,
    "memory_read": _READ,
    "remind_me": _WRITE,
    "remind_list": _READ,
    "remind_cancel": _WRITE,
    "meme_send": ToolPolicy(read_only=False, public=True, irreversible=True),
}

SIDE_EFFECT_TOOLS = frozenset(name for name, p in BUILTIN_POLICIES.items() if not p.read_only)
IRREVERSIBLE = frozenset(name for name, p in BUILTIN_POLICIES.items() if p.irreversible)
PUBLIC_TOOLS = frozenset(name for name, p in BUILTIN_POLICIES.items() if p.public)
# 保留旧调用方的技能工具策略。技能工具本身未注册给聊天模型。
READ_ONLY_TOOLS = frozenset(name for name, p in BUILTIN_POLICIES.items() if p.read_only) | {
    "skill_read", "skill_list",
}
RETRY_BLOCKED = SIDE_EFFECT_TOOLS | {"skill_save"}
