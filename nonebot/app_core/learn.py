"""[orchestration] 学习层：复盘、周报、间隔重复、技能、笔记、好奇/纠正清单。"""
from __future__ import annotations

import json
import logging
import math
import re
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

from app_core import paths as _paths

from app_core import profile as profile_mod
from app_core.learn_store import (  # noqa: F401
    CHAT_DB, CORRECTIONS, CURIOUS, LEARN, MEM_DB, NOTES, PROFILE,
    PROFILE_JSON, RAW, SKILLS, _CURIOUS_BAD, _clean_key, _grams, _list_key,
    apply_review, curious_ok, dedupe_items, merge_list, normalize_correction,
    note_list, profile_conf, profile_fields, profile_path, profile_previous,
    profile_seen, read_list, recent_corrections, resolve_curious, save_profile,
    too_similar, write_list,
)
from app_core.learn_constants import (  # noqa: F401
    SYSTEM_PROFILE, SYSTEM_RECALL, SYSTEM_REVIEW, SYSTEM_WEEKLY, _PROFILE_SPEC,
)
from app_core.notes_meta import (  # noqa: F401
    META_KEYS, _meta_field, note_label, note_meta, note_project,
    page_label, page_meta, parse_ts, set_meta,
)

log = logging.getLogger("qqbot.learn")

# 结构化画像：md 继续留着（kb 索引 / review 回读都用它），json 是机器读的那份
# 给模型看的字段模板：从 FIELDS 生成，避免两边写死两处、改一处漏一处
# confidence 用 0~1：你从这几天的对话里**确实知道**才给 0.8 以上；
# 只是印象、或者从旧画像抄下来的，给 0.5 左右。低于 0.6 的字段不会被念出去。
# evidence 只写**今天这段对话里**他亲口说过的话。今天没聊到这个字段就写空字符串——
# 空表示「沿用旧值、但没有新证据」，这个字段会随时间自然过期。
# 编造证据比没有证据更糟：旧结论会一直生效，除非它过期。
# changed 判的是「今天这句有没有提出**新的或修正后**的信息」：
#   true  = 新信息、或者和旧画像不一致（换工作、搬家、改了偏好）；
#   false = 只是在复述旧画像里已有的结论，措辞变了不算。
# 判 false 时程序会**保留旧文本**，只按你给的 confidence 记账——所以别为了凑数改措辞。
# ---------- 清单读写 ----------


# 「没搞懂」vs「信息缺口」。后者问出来很越界：「你家庭情况还没提过」不是问题。
# ---------- 间隔重复 ----------
def review_candidates(min_importance: float = 0.6, min_age_days: float = 3,
                      limit: int = 5) -> list[tuple]:
    """按「重要性 × 遗忘程度」挑要复习的原子事实。

    上一版按 importance DESC 排序，而 reinforce 每次又往上加，
    头部几条会一直霸榜、永远复习那几条。现在用分数：
    importance × exp(-距上次强化天数 / 30)，复习过就掉分，自然轮到别的。
    """
    now = time.time()
    cutoff = now - min_age_days * 86400
    try:
        con = sqlite3.connect(f"file:{MEM_DB}?mode=ro", uri=True, timeout=10)
        rows = con.execute(
            "SELECT id, content, importance, COALESCE(last_reinforced_at,0) FROM memory_atoms "
            "WHERE status='active' AND importance >= ? AND COALESCE(last_reinforced_at,0) < ? "
            "LIMIT 500", (min_importance, cutoff)).fetchall()
        con.close()
    except Exception:
        return []
    scored = []
    for rid, content, imp, last in rows:
        imp = float(imp or 0.5)
        days = (now - float(last)) / 86400.0 if last else 999.0
        scored.append((imp * math.exp(-max(0.0, days) / 30.0), rid, content, imp))
    scored.sort(key=lambda x: -x[0])
    return [(r[1], r[2], r[3]) for r in scored[:limit]]


def reinforce(ids: list[int], boost: float = 0.02) -> int:
    """复习一次：记次数、更新时间、小幅提权。

    提权改用「朝 1.0 逼近」的形式，避免每次 +0.02 让老条目无限涨到 1.0。
    """
    if not ids:
        return 0
    now = time.time()
    try:
        con = sqlite3.connect(MEM_DB, timeout=10)
        con.execute("PRAGMA journal_mode=WAL")
        with con:
            for i in ids:
                con.execute(
                    "UPDATE memory_atoms SET reinforcement_count=COALESCE(reinforcement_count,0)+1,"
                    "last_reinforced_at=?, last_accessed_at=?, "
                    "importance=MIN(1.0, importance + ? * (1.0 - importance)) "
                    "WHERE id=?", (now, now, boost, i))
        con.close()
        return len(ids)
    except Exception:
        return 0


async def spaced_review(core, limit: int = 5) -> str:
    rows = review_candidates(limit=limit)
    if not rows:
        return "没有需要复习的记忆"
    ids = [int(r[0]) for r in rows]
    n = reinforce(ids)
    lines = [f"{i+1}. {str(r[1])[:300]}" for i, r in enumerate(rows)]
    text = await core.complete_summary(
        [{"role": "system", "content": SYSTEM_RECALL},
         {"role": "user", "content": "这些是很久没想起的记忆：\n" + "\n".join(lines) + "\n\n写一段回忆。"}],
        max_tokens=500,
    )
    day = datetime.now().strftime("%Y-%m-%d")
    body = [f"# {day} 回忆\n", f"\n今天复习了 {n} 条记忆。\n"]
    body.append(f"\n{text}\n" if text else "\n" + "\n".join(f"- {r[1][:200]}" for r in rows) + "\n")
    LEARN.mkdir(parents=True, exist_ok=True)
    (LEARN / f"recall-{day}.md").write_text("".join(body), encoding="utf-8")
    return f"复习了 {n} 条记忆"


# ---------- 技能 ----------
def skill_save(name: str, trigger: str, steps: str) -> str:
    name = (name or "").strip()[:40]
    if not name:
        return "技能名不能为空"
    SKILLS.mkdir(parents=True, exist_ok=True)
    text = (f"# {name}\n\n触发：{trigger or name}\n"
            f"更新时间：{datetime.now():%Y-%m-%d %H:%M}\n\n{steps.strip()}\n")
    p = SKILLS / f"{name}.md"
    p.write_text(text, encoding="utf-8")
    _touch_wiki(p)
    return f"技能已保存：{name}"


def skill_list() -> list[str]:
    try:
        return sorted(p.stem for p in SKILLS.glob("*.md"))
    except Exception:
        return []


def skill_read(name: str) -> str:
    p = SKILLS / f"{name}.md"
    return p.read_text(encoding="utf-8")[:3000] if p.exists() else f"没有这个技能：{name}"


def skill_match(text: str, max_skills: int = 2) -> list[str]:
    hits = []
    for p in SKILLS.glob("*.md") if SKILLS.exists() else []:
        try:
            raw = p.read_text(encoding="utf-8")
        except Exception:
            continue
        m = re.search(r"^触发[：:]\s*(.+)$", raw, re.M)
        if not m:
            continue
        triggers = [x.strip() for x in re.split(r"[,，、\s]+", m.group(1)) if x.strip()]
        score = sum(1 for t in triggers if t and t in text)
        if score:
            hits.append((score, raw))
    hits.sort(key=lambda x: -x[0])
    return [h[1] for h in hits[:max_skills]]


# ---------- 笔记 ----------
def note_write(title: str, content: str, tag: str = "", project: str = "",
               source: str = "自撰", stale_days: int = 0) -> str:
    """写学习笔记。project 会写进文件头，知识库按项目过滤时用得上。

    stale_days：这条知识多久会过时（天），**由写笔记的模型自己判断**，
    0 表示长期有效。写进文件头，索引和 kb_read 据此提醒「可能过时」。
    为什么不让关键词判：字面匹配分不清「价格」是信息还是方法论（实测误判率 2/3）。
    """
    title = (title or "").strip()[:60] or "未命名笔记"
    content = (content or "").strip()
    if not content:
        return "内容为空"
    slug = re.sub(r"[^\w\u4e00-\u9fa5-]+", "-", title).strip("-")[:30] or "note"
    NOTES.mkdir(parents=True, exist_ok=True)
    now = datetime.now()
    # 同名标题跨天也写进同一页：知识应该沉淀，不是每天新建一份
    same = sorted(NOTES.glob(f"*{slug}.md"))
    p = same[-1] if same else NOTES / f"{now:%Y-%m-%d}-{slug}.md"
    if p.exists():
        with p.open("a", encoding="utf-8") as f:
            # 补充段带完整日期：老的只写「## 15:30 补充」，跨天以后根本不知道是哪天补的
            f.write(f"\n\n## {now:%Y-%m-%d %H:%M} 补充（未验证）\n\n{content}\n")
        _touch_wiki(p)
        return f"已追加到 {p.name}"
    proj = (project or "").strip()[:20]
    if not proj:
        t = (tag or "").strip()
        proj = t if t and t not in ("未分类", "网页学习") else "通用"
    p.write_text(
        f"# {title}\n\n- 日期：{now:%Y-%m-%d %H:%M}\n- 标签：{tag or '未分类'}\n"
        f"- 项目：{proj}\n- 来源：{source or '自撰'}\n- 最后验证：未验证\n- 状态：active\n"
        f"- 保质期：{int(stale_days)}天\n\n"
        f"{content}\n",
        encoding="utf-8")
    _touch_wiki(p)
    return f"已记下：{p.name}"


def _touch_wiki(path: Path) -> None:
    """写完笔记立刻在索引里占位（写穿）。

    延迟导入是为了避开循环依赖：wiki 在模块级 import learn，
    而这里只在真正写完的那一刻才需要 wiki。

    不做的话：note_write 只落文件，索引要等下一小时 refresh 才更新——
    模型刚写完的知识，在下一轮 kb_read 打开会说「没找到这一页」。
    """
    try:
        from app_core import wiki as _wiki

        _wiki.upsert_now(path)
    except Exception:
        pass


# ---------- 对话原文归档 ----------
def _day_messages(day: str, session: str = "") -> list[tuple[str, str, float]]:
    """当天原文。session 非空时只取这个会话，别把陌生人的话归档成他的原文。"""
    if not session:
        try:
            from app_core import config
            session = config.owner_session()
        except Exception:  # noqa: BLE001
            session = ""
    sql = ("SELECT role, content, ts FROM messages "
           "WHERE date(ts, 'unixepoch', 'localtime') = ?")
    args: list = [day]
    if session:
        sql += " AND session=?"
        args.append(session)
    sql += " ORDER BY id"
    con = sqlite3.connect(CHAT_DB, timeout=15)
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


def archive_raw(day: str = "", session: str = "") -> str:
    """把某天的对话原文落盘到 `learn/raw/YYYY-MM-DD.md`。

    为什么留原文：笔记、复盘、记忆全是模型二次加工的结果，出问题时要能回到
    「他当时到底说了什么」。这份不加工，也让 kb_search 能搜到原始说法，
    而不是只搜到模型的转述。

    默认归档昨天（凌晨跑，当天已经完整），重复调用是覆盖，可以放心重跑。
    """
    day = day or (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    if not session:
        try:
            from app_core import config
            session = config.owner_session()
        except Exception:  # noqa: BLE001
            session = ""
    try:
        rows = _day_messages(day, session=session)
    except Exception as e:  # noqa: BLE001
        return f"读会话库失败：{type(e).__name__}: {e}"
    if not rows:
        return f"{day} 没有对话记录"
    lines = [f"# {day} 对话原文", ""]
    kept = 0
    for role, content, ts in rows:
        text = (content or "").strip()
        if not text:
            continue
        who = "我" if role == "assistant" else "他"
        try:
            hhmm = datetime.fromtimestamp(float(ts)).strftime("%H:%M")
        except Exception:
            hhmm = "--:--"
        lines.append(f"[{hhmm}] {who}：{text}")
        kept += 1
    if not kept:
        return f"{day} 没有可归档的内容"
    RAW.mkdir(parents=True, exist_ok=True)
    (RAW / f"{day}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return f"已归档 {day}：{kept} 条 → raw/{day}.md"


# ---------- 复盘 / 周报（实现已拆到 review.py） ----------
# 2026-11-16：只保留两个公开入口（命令与调度用）。
# 原本还有 quality_metrics / _extract_json / _documents_since / _day_context 四个透传，
# 它们是 review 的内部实现，透传出去等于把 review 的结构焊进 learn 的 API；
# 调用方已改为直接 import review。
async def review(core) -> str:
    from app_core import review as _review_mod

    return await _review_mod.review(core)


async def weekly(core) -> str:
    from app_core import review as _review_mod

    return await _review_mod.weekly(core)
