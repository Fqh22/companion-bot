"""复盘与周报：调模型、算客观指标、落盘。

从 learn.py 拆出来（2026-11-06）。理由是改动节奏：提示词几乎每周调，
而 learn.py 里另外几件事（笔记 / 技能 / 间隔重复 / 清单）几乎不动。
拆开后调 prompt 不用在 986 行里翻。

**不调模型的写入规则仍在 learn.py**（learn.apply_review / normalize_correction）——
那一半是可离线测的纯逻辑，和「调模型」分开才对。
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from datetime import datetime, timedelta

from app_core import profile as profile_mod
from app_core import learn
from app_core.learn import (SYSTEM_PROFILE, SYSTEM_REVIEW, SYSTEM_WEEKLY,
                            _PROFILE_SPEC)

log = logging.getLogger("qqbot.review")


def _profile_block_for_review() -> str:
    """给复盘模型看的当前画像：每条都带上「多久没证据、置信度、上次是什么」。

    为什么要带年龄：不带的话，模型看到旧画像只会照着抄；带上「这条 40 天没证据了」，
    它才有机会判断「该更新还是该保留」。上次的值也一并给，渐变（换了方向但没明说）
    才能被看见。
    """
    fields = learn.profile_fields()
    if not fields:
        try:
            return learn.PROFILE.read_text(encoding="utf-8").strip() if learn.PROFILE.exists() else ""
        except Exception:  # noqa: BLE001
            return ""
    conf, seen, prev = learn.profile_conf(), learn.profile_seen(), learn.profile_previous()
    lines = []
    for k in profile_mod.FIELDS:
        v = fields.get(k)
        if not v:
            continue
        d = profile_mod.age_days(seen.get(k))
        age = "刚有证据" if d is None else (f"{d:.0f} 天前" if d >= 1 else "今天")
        tail = ""
        p = prev.get(k) or {}
        if p.get("text"):
            pd = profile_mod.age_days(p.get("at"))
            when = "最近" if (pd is None or pd < 1) else f"{pd:.0f} 天前"
            tail = f"｜上一次是「{p['text'][:18]}」（{when}改的）"
        lines.append(f"- {k}：{v}（{age}有证据，置信度 "
                     f"{float(conf.get(k, 0.7)):.2f}{tail}）")
    return "\n".join(lines)
def _extract_json(text: str) -> dict:
    t = (text or "").strip()
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", t, re.S)
    if m:
        t = m.group(1)
    else:
        i, j = t.find("{"), t.rfind("}")
        if i >= 0 and j > i:
            t = t[i:j + 1]
    try:
        d = json.loads(t)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}
# ---------- 记忆读取 ----------
def _documents_since(days: float) -> list[str]:
    since = datetime.now() - timedelta(days=days)
    try:
        con = sqlite3.connect(f"file:{learn.MEM_DB}?mode=ro", uri=True, timeout=10)
        rows = con.execute("SELECT text, created_at FROM documents ORDER BY id DESC LIMIT 200").fetchall()
        con.close()
    except Exception:
        return []
    out = []
    for text, created in rows:
        try:
            dt = datetime.strptime(str(created)[:19], "%Y-%m-%d %H:%M:%S")
        except Exception:
            continue
        if dt >= since and (text or "").strip():
            out.append(text.strip())
    return out
# ---------- 复盘 ----------
def _owner(session: str = "") -> str:
    """主人会话。不传就按 config 推；推不出来返回空串（不过滤）。"""
    if session:
        return session
    try:
        from app_core import config
        return config.owner_session()
    except Exception:  # noqa: BLE001
        return ""


def _day_context(days: float = 1, session: str = "") -> str:
    """复盘/周报的补充信号：情绪、对话量、提醒完成情况。

    原来只看记忆文档，等于只看到「结论」，看不到「那天是什么状态」。
    """
    parts = []
    session = _owner(session)
    try:
        from app_core import mood

        # 先 settle 再读：凌晨复盘用的是「此刻」的状态，而不是上一条消息时的快照。
        # 不 settle 的话，03:40 的复盘会把昨晚 23:50 的情绪当成今天的状态写进学习资料。
        m = mood.settle(mood.load())
        parts.append("情绪：心情 %+.2f，精力 %.2f，互动 %.2f"
                     % (m.get("valence", 0), m.get("energy", 0), m.get("social", 0)))
    except Exception:
        pass
    since = time.time() - days * 86400
    try:
        con = sqlite3.connect(f"file:{learn.CHAT_DB}?mode=ro", uri=True, timeout=10)
        if session:
            n = con.execute("SELECT COUNT(*) FROM messages WHERE ts>=? AND session=?",
                            (since, session)).fetchone()[0]
        else:
            n = con.execute("SELECT COUNT(*) FROM messages WHERE ts>=?",
                            (since,)).fetchone()[0]
        con.close()
        parts.append(f"对话量：{n} 条消息")
    except Exception:
        pass
    try:
        con = sqlite3.connect(f"file:{learn.CHAT_DB}?mode=ro", uri=True, timeout=10)
        rows = con.execute(
            "SELECT text, sent FROM reminders WHERE due_ts>=? ORDER BY due_ts",
            (since,)).fetchall()
        con.close()
        if rows:
            done = sum(1 for _, s in rows if s)
            parts.append(f"提醒：{len(rows)} 条到期，已发出 {done}")
            for text, sent in rows[:5]:
                parts.append(f"  - {text}（{'已提醒' if sent else '未提醒'}）")
    except Exception:
        pass
    return "\n".join(parts)
def _session_of(core) -> str:
    """从 core 取主人会话；测试桩没有这个方法时返回空串（不过滤）。"""
    try:
        return core.private_session()
    except Exception:  # noqa: BLE001
        return ""


async def review(core) -> str:
    today = datetime.now().strftime("%Y-%m-%d")
    session = _session_of(core)
    memories = _documents_since(1)
    if not memories:
        return f"{today} 没有新记忆，跳过复盘"
    profile = _profile_block_for_review()
    curious_now = learn.read_list(learn.CURIOUS)
    joined = "\n".join(f"- {m[:220]}" for m in memories[:40])
    prompt = (
        f"【我现在的自我认知】\n{profile or '（还没有）'}\n\n"
        f"【{today} 的状态】\n{_day_context(1, session) or '（没有记录）'}\n\n"
        f"【{today} 的记忆】\n{joined}\n\n"
        f"【我还没搞懂的事】\n" + ("\n".join(f"- {x}" for x in curious_now) or "（没有）") + "\n\n"
        f"【已经记下的纠正（别重复写——换个说法也算重复）】\n"
        + ("\n".join(f"- {x}" for x in learn.read_list(learn.CORRECTIONS)[-24:]) or "（还没有）") + "\n\n"
        "请输出 JSON，不要输出别的：\n"
        '{"learned":"3-6 条可复用的判断规则，写成「当…时，做…（因为…）」或「别做…，因为…」；不要写「今天聊了…」这种流水",'
        f'"profile":{_PROFILE_SPEC},'
        '"corrections":["他明确纠正过我的、或者明确不喜欢我的做法，每条一句，'
        '开头必须带场景标签：[图片] 看图/识图判断错，[文字] 说话方式或内容错，[通用] 其他；'
        '没有就空数组"],'
        '"curious":["他明确提到过、但当时没讲清楚的具体内容（有原话可指），每条一句；'
        '不要写「还没提过/没讲过/一直没定」这类信息缺口；没有就空数组"],'
        '"resolved_curious":["今天已经搞懂的、上面「我还没搞懂的事」里的条目；没有就空数组"]}'
    )
    raw = await core.complete_summary(
        [{"role": "system", "content": SYSTEM_REVIEW + SYSTEM_PROFILE},
         {"role": "user", "content": prompt}],
        max_tokens=900,
    )
    data = _extract_json(raw)
    res = learn.apply_review(data, today)
    if not res["learned"] and not res["profile"]:
        return "复盘失败：模型没有给出可用结果"
    return f"复盘完成（{len(memories)} 条记忆，纠正 {res['corrections']} 条）"
# ---------- 周报 ----------
def quality_metrics(days: int = 7, session: str = "") -> dict:
    """自动算几个「回复好不好」的指标，给周报用。

    为什么要自动算：靠人工巡检会漏，而且事后想不起是哪一周开始变差的。
    全部来自 chat.db 的现成数据，不额外花 token。
    session 非空时只统计这个会话——陌生人的私聊不该左右他的周报。
    """
    cut = time.time() - days * 86400
    session = _owner(session)
    # 同一段 SQL 模板，有 session 就多一个 AND。参数顺序别搞反。
    and_s = " AND session=?" if session else ""
    arg_s: tuple = (session,) if session else ()
    out: dict = {}
    try:
        conn = sqlite3.connect(learn.CHAT_DB, timeout=10)
        try:
            u = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE role='user' AND ts>?" + and_s,
                (cut, *arg_s)).fetchone()[0]
            a, alen = conn.execute(
                "SELECT COUNT(*), COALESCE(AVG(LENGTH(content)),0) FROM messages"
                " WHERE role='assistant' AND ts>?" + and_s, (cut, *arg_s)).fetchone()
            ulen = conn.execute(
                "SELECT COALESCE(AVG(LENGTH(content)),0) FROM messages"
                " WHERE role='user' AND ts>?" + and_s, (cut, *arg_s)).fetchone()[0]
            dup = conn.execute(
                "SELECT COUNT(*) FROM (SELECT REPLACE(REPLACE(content,'？',''),'。','') k,"
                " COUNT(*) c FROM messages WHERE role='user' AND ts>?" + and_s +
                " GROUP BY k HAVING c>1)", (cut, *arg_s)).fetchone()[0]
            tok = conn.execute(
                "SELECT COALESCE(SUM(prompt_tokens+completion_tokens),0) FROM usage"
                " WHERE ts>?", (cut,)).fetchone()[0]
            out = {
                "用户消息": int(u or 0), "回复": int(a or 0),
                "平均回复字数": round(float(alen or 0)),
                "平均用户字数": round(float(ulen or 0)),
                "长度比": round(float(alen or 0) / float(ulen or 1), 1),
                "重复提问条数": int(dup or 0),
                "token": int(tok or 0),
            }
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001
        log.debug("指标计算失败：%s", e)
    try:
        out["纠正清单条数"] = len(learn.read_list(learn.CORRECTIONS))
    except Exception:  # noqa: BLE001
        pass
    return out
async def weekly(core) -> str:
    today = datetime.now().strftime("%Y-%m-%d")
    session = _session_of(core)
    memories = _documents_since(7)
    if not memories:
        return "这周没有新记忆，周报跳过"
    profile = learn.PROFILE.read_text(encoding="utf-8").strip() if learn.PROFILE.exists() else ""
    corr = learn.read_list(learn.CORRECTIONS)
    cur = learn.read_list(learn.CURIOUS)
    joined = "\n".join(f"- {m[:220]}" for m in memories[:60])
    prompt = (
        f"【我现在的自我认知】\n{profile or '（还没有）'}\n\n"
        f"【他纠正过我、不能再犯的】\n" + ("\n".join(f"- {x}" for x in corr) or "（没有）") + "\n\n"
        f"【我还没搞懂的事】\n" + ("\n".join(f"- {x}" for x in cur) or "（没有）") + "\n\n"
        f"【这一周的状态】\n{_day_context(7, session) or '（没有记录）'}\n\n"
        f"【这一周（{today} 往前 7 天）的记忆】\n{joined}\n\n"
        "写一份发给主人的记忆周报，并检查这周有没有再犯纠正清单里的错。"
    )
    text = await core.complete_summary(
        [{"role": "system", "content": SYSTEM_WEEKLY},
         {"role": "user", "content": prompt}],
        max_tokens=900,
    )
    if not text:
        return "周报失败：模型没有输出"
    m = quality_metrics(7, session)
    if m:
        text += ("\n\n## 这周的客观指标\n"
                 + "\n".join(f"- {k}：{v}" for k, v in m.items()))
    # 影子这周：七路样本的汇总。之前只有 ops/shadow-eval.py 能手动看，
    # 没人跑就等于没记——现在跟着周报一起出来。
    try:
        from app_core import shadow as shadow_mod

        _snap = shadow_mod.weekly_summary(7)
        _prev = shadow_mod.rotate_snapshot(_snap)
        _sl = shadow_mod.summary_lines(7, prev=_prev)
        if _sl:
            text += ("\n\n## 影子这周（只记录，没改行为）\n"
                     + "\n".join("- " + x for x in _sl))
    except Exception as e:  # noqa: BLE001
        log.debug("影子周汇总失败：%s", e)
    # 运维两件事：异地备份、恢复演练。周级提示，不天天烦。
    try:
        from app_core import maintenance as maint_mod

        _ops = maint_mod.ops_status()
        if _ops:
            text += ("\n\n## 运维\n" + "\n".join("- " + x for x in _ops))
    except Exception as e:  # noqa: BLE001
        log.debug("运维状态汇总失败：%s", e)
    learn.LEARN.mkdir(parents=True, exist_ok=True)
    (learn.LEARN / f"weekly-{today}.md").write_text(text + "\n", encoding="utf-8")
    return text
