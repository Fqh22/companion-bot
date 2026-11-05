"""把一段对话总结成一条长期记忆（结构化 JSON）。"""
from __future__ import annotations

import json
import re

SYSTEM = """你在把一段真实对话整理成一条长期记忆。只输出 JSON，不要解释：
{
 "summary": "120-200 字，第一人称，写清发生了什么、对方在意什么、我该记住什么",
 "key_facts": ["3-6 条关键事实，每条一句话，带具体细节"],
 "topics": ["2-4 个主题词"],
 "followups": ["最多 3 条：他明确说了要做的具体事（有承诺或时间），每条一句；没有就空数组"],
 "importance": 0.0,
 "progress": "advance | repeat | switch",
 "digest": "200-300 字，把【旧纪要】和这段新对话合成一段「更早的对话纪要」"
}
importance：日常寒暄 0.25-0.4；有信息量或明显情绪 0.5-0.7；重要事件/长期约定 0.75-0.95。
followups 只写「有明确承诺或待办」的，比如「周四前交稿」「想换工作但还没投」。
泛泛的打算、随口一提的想法、以及他已经在做的事，都不算；宁少勿多。
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
        return {}
    if not isinstance(data, dict):
        return {}
    out = {
        "summary": str(data.get("summary") or "").strip(),
        "key_facts": [str(x).strip() for x in (data.get("key_facts") or []) if str(x).strip()][:6],
        "topics": [str(x).strip() for x in (data.get("topics") or []) if str(x).strip()][:4],
        "followups": [str(x).strip() for x in (data.get("followups") or []) if str(x).strip()][:4],
    }
    try:
        out["importance"] = max(0.0, min(1.0, float(data.get("importance", 0.5))))
    except Exception:
        out["importance"] = 0.5
    prog = str(data.get("progress") or "").strip().lower()
    out["progress"] = prog if prog in ("advance", "repeat", "switch") else ""
    one_line = " ".join(str(data.get("digest") or "").split())
    out["digest"] = one_line[:600]
    return out
