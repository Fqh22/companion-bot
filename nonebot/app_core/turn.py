"""[domain] 合并并渲染同一轮里的文本、图片、文件和平台元数据。"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Turn:
    """一轮里攒到的全部输入。"""

    texts: list[str] = field(default_factory=list)
    images: list[str] = field(default_factory=list)
    file: tuple[str, str, str] | None = None      # (文件名, 正文, 备注)
    meta: dict = field(default_factory=dict)       # 平台元数据（event_id / user_id / ...）

    def empty(self) -> bool:
        return not (self.texts or self.images or self.file)

    def merge(self, other: "Turn") -> "Turn":
        self.texts.extend(other.texts)
        self.images.extend(other.images)
        if other.file and not self.file:
            self.file = other.file
        if other.meta:
            reply_to = self.meta.get("reply_to")
            quote_text = self.meta.get("quote_text")
            self.meta.update(other.meta)
            if not other.meta.get("reply_to") and reply_to:
                self.meta.update(reply_to=reply_to, quote_text=quote_text)
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
        # 语音转写：形式来自 meta，前缀只在渲染时加（数据层是干净的文本）
        _voice = bool((self.meta or {}).get("voice"))
        if _voice and self.texts and not self.file:
            parts.extend("[语音] " + t for t in self.texts)
            # 只记一份：以前后面的「补充」块会把同一段转写再拼一次，
            # 语音历史的摘要变成「[语音] X 补充：X」。
            stored = "[语音] " + " ".join(self.texts)[:300]
        else:
            parts.extend(self.texts)
        if self.file and stored and self.texts:
            # 文件+补充说明：补充那句也要进历史，否则总结时它凭空消失
            stored += " 补充：" + " ".join(self.texts)[:200]
        if not parts:
            parts.append("[发了图片]")
        return "\n".join(parts), stored
