"""把一段对话总结成一条长期记忆（结构化 JSON）。"""
from __future__ import annotations

import json
import re

from app_core import parse_log

SYSTEM = """你在把一段真实对话整理成一条长期记忆。只输出 JSON，不要解释：
{
 "summary": "120-200 字，第一人称，写清发生了什么、对方在意什么、我该记住什么",
 "key_facts": ["3-6 条关键事实，每条一句话，带具体细节"],
 "topics": ["2-4 个主题词"],
 "followups": ["最多 2 条：他明确说要做的具体事（有承诺或时间），主语必须是他；没有就空数组"],
 "importance": 0.0,
 "progress": "advance | repeat | switch",
 "digest": "200-300 字，把【旧纪要】和这段新对话合成一段「更早的对话纪要」"
}
importance：日常寒暄 0.25-0.4；有信息量或明显情绪 0.5-0.7；重要事件/长期约定 0.75-0.95。
followups 只写「他明确承诺或交代要做的具体事」，比如「周四前交稿」「去闲鱼查 4080S 价格」。
以下一律不写（线上踩过：这些会被当成他的待办天天追问）：
  - 你自己的待办/动作（「我需要整理…」「补讲…」「告诉他…」）
  - 他没回答的问题（「他还没回复…」「他没确认…」）
  - 一次性日程（「明天下午面试」「当天去体检」）——那归提醒
  - 给他的规则/偏好（「以后先搜索再答」）
泛泛的打算、随口一提的想法、他已经在做的事，都不算；宁少勿多，0-2 条最常见。
progress：只看「他的问题解决了没有」，不要猜他满不满意——
  advance = 他在往下推进（追问细节、换下一步）或这件事办完了；
  repeat  = 他换个说法又在问同一件事（说明上一次没答到点上）；
  switch  = 他开了个新话题（上一个问题多半已经解决）。
digest：承接【旧纪要】，保留「在做什么、已定的结论、悬而未决的问题、他的偏好和约定」，
  删掉寒暄、重复和已过时的细节；陈述句，不用第一人称，不要 Markdown 和标题。
不要编造，不确定的不写。"""


def vocab_hint(vocab: list[str]) -> str:
    """把已有主题词拼成 prompt 片段，让模型优先复用而不是自造近义词。

    2026-11-05 实测：410 个标签里 393 个只出现过一次，同一件事被拆成
    「harness」「harness 架构」「DSH」——实体连边因此几乎连不起来。
    """
    items = [str(v).strip() for v in (vocab or []) if str(v).strip()][:50]
    if not items:
        return ""
    return (
        "【已有主题词】我们之前用过这些标签：\n"
        + "、".join(items)
        + "\n写 topics 时**优先复用上面的词**：能对上就直接写原词，"
          "不要加修饰语（有「求职」就不要写「求职面试」）。"
          "确实对不上再新建，新建的标签要短，2–6 个字。"
    )


def format_dialog(rows: list[tuple[str, str]]) -> str:
    lines = []
    for role, content in rows:
        who = "他" if role == "user" else "我"
        text = (content or "").strip().replace("\n", " ")[:400]
        if text:
            lines.append(f"[{who}] {text}")
    return "\n".join(lines)


def _as_list(value, limit: int, field: str, raw: str) -> list[str]:
    """模型偶尔把数组写成字符串。

    `for x in "他讨厌空话"` 会逐字符迭代，存成 6 条单字事实——而且不报错。
    字符串当一项；其它非列表直接丢弃，两种情况都记一条 parse_fail。
    """
    if isinstance(value, str):
        if value.strip():
            parse_log.note("summary", raw, field + "=str")
            return [value.strip()][:limit]
        return []
    if not isinstance(value, (list, tuple)):
        if value is not None:
            parse_log.note("summary", raw, field + "=" + type(value).__name__)
        return []
    out = [str(x).strip() for x in value
           if isinstance(x, (str, int, float)) and str(x).strip()]
    if len(out) != len(value):
        parse_log.note("summary", raw, field + "=mixed")
    return out[:limit]


def parse(text: str) -> dict:
    t = (text or "").strip()
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", t, re.S)
    if m:
        t = m.group(1)
    else:
        i, j = t.find("{"), t.rfind("}")
        if i >= 0 and j > i:
            t = t[i:j + 1]
    try:
        data = json.loads(t)
    except Exception:
        parse_log.note("summary", text, "json")
        return {}
    if not isinstance(data, dict):
        parse_log.note("summary", text, "type=" + type(data).__name__)
        return {}
    _summary = data.get("summary")
    if _summary is not None and not isinstance(_summary, str):
        parse_log.note("summary", text, "summary=non-str")
        _summary = str(_summary) if isinstance(_summary, (int, float)) else ""
    out = {
        "summary": str(_summary or "").strip(),
        "key_facts": _as_list(data.get("key_facts"), 6, "key_facts", text),
        "topics": _as_list(data.get("topics"), 4, "topics", text),
        "followups": _as_list(data.get("followups"), 4, "followups", text),
    }
    try:
        out["importance"] = max(0.0, min(1.0, float(data.get("importance", 0.5))))
    except Exception:
        out["importance"] = 0.5
    prog = str(data.get("progress") or "").strip().lower()
    out["progress"] = prog if prog in ("advance", "repeat", "switch") else ""
    _digest = data.get("digest")
    if _digest is not None and not isinstance(_digest, str):
        parse_log.note("summary", text, "digest=non-str")
        _digest = ""
    one_line = " ".join(str(_digest or "").split())
    out["digest"] = one_line[:600]
    return out
