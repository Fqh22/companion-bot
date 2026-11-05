"""一轮对话的输入聚合：把「图片/文件 + 补充的一句话」并成同一轮。

为什么单独一层：什么时候算一轮、哪些内容该并进来、给模型的正文怎么拼——
这些是纯状态操作。放在插件里就只能靠真实 QQ 消息验证，而插件又不许 import
nonebot 以外的东西。抽出来之后能离线回归。

对应线上踩到的坑：图片和文件原来走「立即处理并 return」的近路，绕过防抖，
于是「发张图 + 补一句『这是昨天的报错』」被拆成两条互不知情的回复。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Turn:
    """一轮里攒到的全部输入。"""

    texts: list[str] = field(default_factory=list)
    images: list[str] = field(default_factory=list)
    file: tuple[str, str, str] | None = None      # (文件名, 正文, 备注)

    def empty(self) -> bool:
        return not (self.texts or self.images or self.file)

    def merge(self, other: "Turn") -> "Turn":
        self.texts.extend(other.texts)
        self.images.extend(other.images)
        if other.file and not self.file:
            self.file = other.file
        return self

    def compose(self) -> tuple[str, str | None]:
        """拼成 (给模型看的正文, 入库用的摘要)。

        摘要只留前 300 字正文：否则接下来几十轮都会把 8000 字重发一遍。
        """
        parts: list[str] = []
        stored: str | None = None
        if self.file:
            name, text, note = self.file
            parts.append(
                f"[用户发来一个文件：{name}，{note}] 文件内容：\n\n{text[:8000]}\n\n"
                "请根据文件内容回答；如果内容被截断，说明你只看到了一部分。")
            stored = (f"[发来文件：{name}，{len(text)} 字"
                      f"{('，' + note) if note else ''}] {text[:300]}…")
        parts.extend(self.texts)
        if stored and self.texts:
            # 文件+补充说明：补充那句也要进历史，否则总结时它凭空消失
            stored += " 补充：" + " ".join(self.texts)[:200]
        if not parts:
            parts.append("[发了图片]")
        return "\n".join(parts), stored


class TurnQueue:
    """按会话隔离的待处理输入。

    `hits` 是「这个会话收到过多少条输入」的计数器，用来判断在某个短窗口内
    有没有新的东西进来（图片/文件之后等他补一句话）。
    """

    def __init__(self) -> None:
        self._pending: dict[str, Turn] = {}
        self._hits: dict[str, int] = {}

    def add(self, sid: str, *, text: str | None = None,
            images: list[str] | None = None,
            file: tuple[str, str, str] | None = None) -> None:
        turn = self._pending.setdefault(sid, Turn())
        if text:
            turn.texts.append(text)
        if images:
            turn.images.extend(images)
        if file:
            turn.file = file
        self._hits[sid] = self._hits.get(sid, 0) + 1

    def peek(self, sid: str) -> Turn | None:
        return self._pending.get(sid)

    def has(self, sid: str) -> bool:
        turn = self._pending.get(sid)
        return bool(turn and not turn.empty())

    def take(self, sid: str) -> Turn:
        """取走这一轮并清空待处理区。"""
        turn = self._pending.get(sid) or Turn()
        self._pending[sid] = Turn()
        return turn

    def hits(self, sid: str) -> int:
        return self._hits.get(sid, 0)

    def drop(self, sid: str) -> None:
        self._pending.pop(sid, None)
        self._hits.pop(sid, None)
