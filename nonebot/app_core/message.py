"""[domain] 表达单元：入站/出站/中间态用同一种 Part 表达「形式」。

为什么：原来形式是「主内容 + 若干可选字段」（text / images / audio / files），
每加一种形式就要在三个结构体各加一个字段、在通道层再写一条分支。
形式一旦变成数据，加「画画」只需要多一个 form，不需要改协议。

Part 是不可变的：它描述「这一块是什么」，不承载「怎么发」。
"""
from __future__ import annotations

from dataclasses import dataclass, field

# 已知形式。不在这里的值也能构造（向前兼容），但通道和降级规则只认这些。
FORMS = ("text", "image", "voice", "file", "drawing")


@dataclass(frozen=True)
class Part:
    form: str
    content: str = ""
    meta: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.form:
            raise ValueError("Part.form 不能为空")

    @property
    def is_media(self) -> bool:
        """需要通道上传/转码的形式（text 之外的都算）。"""
        return self.form != "text"

    def __str__(self) -> str:
        return self.content


def text_parts(chunks) -> list[Part]:
    return [Part("text", str(c)) for c in (chunks or []) if str(c or "").strip()]


def parts_text(parts) -> str:
    """把若干 text Part 拼回一段文字（渲染、落库都靠它）。"""
    return "".join(p.content for p in (parts or []) if p.form == "text")
