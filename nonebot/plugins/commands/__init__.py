"""commands 插件：管理命令。"""
from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

from nonebot import on_command
from nonebot.adapters.onebot.v11 import MessageEvent, MessageSegment
from nonebot.permission import SUPERUSER

from app_core import diary, kb, learn, meme, mood, remind, wiki
from plugins.chat import core

NL = chr(10)

mood_cmd = on_command("mood", priority=5, block=True, permission=SUPERUSER)
mood_hist = on_command("mood_history", priority=5, block=True, permission=SUPERUSER)
remind_list_cmd = on_command("remind_list", priority=5, block=True, permission=SUPERUSER)
remind_cancel_cmd = on_command("remind_cancel", priority=5, block=True, permission=SUPERUSER)
diary_show = on_command("diary_show", priority=5, block=True, permission=SUPERUSER)
diary_write = on_command("diary_write", priority=5, block=True, permission=SUPERUSER)
diary_list = on_command("diary_list", priority=5, block=True, permission=SUPERUSER)
review_now = on_command("review_now", priority=5, block=True, permission=SUPERUSER)
weekly_now = on_command("weekly_now", priority=5, block=True, permission=SUPERUSER)
skill_list = on_command("skill_list", priority=5, block=True, permission=SUPERUSER)
note_list = on_command("note_list", priority=5, block=True, permission=SUPERUSER)
kb_reindex = on_command("kb_reindex", priority=5, block=True, permission=SUPERUSER)
meme_cmd = on_command("meme", priority=5, block=True, permission=SUPERUSER)
meme_stats = on_command("meme_stats", priority=5, block=True, permission=SUPERUSER)
usage_cmd = on_command("usage", priority=5, block=True, permission=SUPERUSER)
cleanup_cmd = on_command("memory_cleanup", priority=5, block=True, permission=SUPERUSER)
restore_cmd = on_command("memory_restore", priority=5, block=True, permission=SUPERUSER)
conflicts_cmd = on_command("memory_conflicts", priority=5, block=True, permission=SUPERUSER)
mem_status_cmd = on_command("mem_status", priority=5, block=True, permission=SUPERUSER)
prompt_last_cmd = on_command("prompt_last", priority=5, block=True, permission=SUPERUSER)
kb_verify = on_command("kb_verify", priority=5, block=True, permission=SUPERUSER)
kb_deprecate = on_command("kb_deprecate", priority=5, block=True, permission=SUPERUSER)
raw_cmd = on_command("raw_archive", priority=5, block=True, permission=SUPERUSER)


def _arg(event: MessageEvent, cmd: str) -> str:
    return (event.get_plaintext() or "").replace(cmd, "", 1).strip()


@mood_cmd.handle()
async def _mood():
    s = mood.settle(mood.load())
    mood.save(s)
    why = [x.get("text") for x in (s.get("why") or [])[-2:] if x.get("text")]
    text = (f"心情 {s['valence']:+.2f}（这阵子 {s.get('valence_base', 0.0):+.2f}） / "
            f"精力 {s['energy']:.2f} / 互动 {s['social']:.2f}")
    if why:
        text += "\n最近：" + "；".join(why)
    await mood_cmd.finish(text)


@mood_hist.handle()
async def _(event: MessageEvent):
    """看最近几天的情绪，用来判断参数调得对不对。"""
    arg = _arg(event, "/mood_history").strip()
    days = int(arg) if arg.isdigit() else 7
    days = max(1, min(60, days))
    rows = mood.recent(days)
    if not rows:
        await mood_hist.finish("还没有情绪历史（每天 23:55 存一行）")
    lines = [f"情绪最近 {len(rows)} 天（心情 / 基线 / 精力 / 互动 / 消息）："]
    for r in rows[-10:]:
        lines.append(f"{r['day'][5:]} {r['valence']:+.2f} / {r['valence_base']:+.2f}"
                     f" / {r['energy']:.2f} / {r['social']:.2f} / {r['msgs']}")
    sm = mood.summary(days)
    lines.append(f"均值 心情 {sm['valence']:+.2f} / 精力 {sm['energy']:.2f}"
                 f" / 互动 {sm['social']:.2f}，共 {sm['msgs']} 条消息")
    lines.append(f"最好 {sm['best']['day'][5:]}（{sm['best']['valence']:+.2f}）"
                 f"，最差 {sm['worst']['day'][5:]}（{sm['worst']['valence']:+.2f}）")
    await mood_hist.finish(NL.join(lines))


@remind_list_cmd.handle()
async def _():
    await remind_list_cmd.finish(remind.describe_pending())


@remind_cancel_cmd.handle()
async def _(event: MessageEvent):
    """取消一条还没到点的提醒（编号来自 /remind_list）。"""
    arg = _arg(event, "/remind_cancel").strip()
    if not arg.isdigit():
        await remind_cancel_cmd.finish(
            "用法：/remind_cancel 编号（先 /remind_list 看方括号里的数字）")
    await remind_cancel_cmd.finish(remind.cancel(int(arg)))


@diary_show.handle()
async def _(event: MessageEvent):
    arg = _arg(event, "/diary_show")
    day = arg if arg else (diary.list_days(1)[0] if diary.list_days(1) else "")
    await diary_show.finish(diary.read(day) if day else "还没有日记")


@diary_list.handle()
async def _():
    days = diary.list_days(7)
    await diary_list.finish(NL.join(days) if days else "还没有日记")


@diary_write.handle()
async def _(event: MessageEvent):
    arg = _arg(event, "/diary_write")
    day = arg or datetime.now().strftime("%Y-%m-%d")
    await diary_write.finish(await diary.write_day(core, day, force=True))


@review_now.handle()
async def _():
    await review_now.finish(await learn.review(core))


@weekly_now.handle()
async def _():
    text = await learn.weekly(core)
    await weekly_now.finish(text[:1500])


@skill_list.handle()
async def _():
    items = learn.skill_list()
    await skill_list.finish(NL.join(items) if items else "还没有技能")


@note_list.handle()
async def _():
    items = learn.note_list()
    await note_list.finish(NL.join(items) if items else "还没有学习笔记")


@kb_reindex.handle()
async def _():
    n = await kb.index()
    await kb_reindex.finish(f"知识库索引完成，新增/更新 {n} 条")


@meme_cmd.handle()
async def _(event: MessageEvent):
    arg = _arg(event, "/meme")
    path, mode, score = await meme.find_ex(arg)
    if not path:
        await meme_cmd.finish("表情包库是空的")
    try:
        # bytes → base64；传 Path 会变成 file:// 宿主机路径，容器里读不到
        await meme_cmd.send(MessageSegment.image(Path(path).read_bytes()))
    except Exception as e:  # noqa: BLE001
        await meme_cmd.finish(f"发送失败：{type(e).__name__}: {e}")


@meme_stats.handle()
async def _():
    s = meme.stats()
    cats = "、".join(f"{k}:{v}" for k, v in sorted(s["categories"].items()))
    hits = s.get("hits") or {}
    total_hits = sum(hits.values()) or 0
    rate = f"{100 * hits.get('semantic', 0) / total_hits:.0f}%" if total_hits else "—"
    await meme_stats.finish(
        f"共 {s['total']} 张（描述 {s['described']}，向量 {s['vectorized']}，"
        f"24h 内发过 {s['recent_24h']}）{NL}"
        f"本次启动以来：语义 {hits.get('semantic', 0)} / 分类 "
        f"{hits.get('category', 0)}，语义命中率 {rate}{NL}{cats}")


@usage_cmd.handle()
async def _():
    d1 = await core.usage_stats(1)
    d7 = await core.usage_stats(7)
    lines = [
        f"今日：{d1['calls']} 次，in {d1['prompt']} / out {d1['completion']} tokens ≈ ¥{d1['cost']}",
        f"近 7 天：{d7['calls']} 次，in {d7['prompt']} / out {d7['completion']} tokens ≈ ¥{d7['cost']}",
        f"缓存命中：{d7['hit_rate'] * 100:.0f}%（{d7['cache_hit']} / {d7['cache_hit'] + d7['cache_miss']} tok）",
        "按用途：" + "；".join(
            f"{name} {calls} 次 in {pt} / out {ct}"
            for name, calls, pt, ct in (d7.get("purposes") or [])[:5]),
        f"平均耗时：{d7['latency']} ms",
        "（agentd 的 token：/agent_usage）",
    ]
    for model, calls, pt, ct, cost in d7["models"]:
        lines.append(f"  {model}: {calls} 次, in {pt or 0} / out {ct or 0} ≈ ¥{cost}")
    await usage_cmd.finish(NL.join(lines))


@cleanup_cmd.handle()
async def _():
    r = core.memory.cleanup()
    await cleanup_cmd.finish(f"已归档 {r['archived']} 条，剩余活跃 {r['alive']} 条")


@restore_cmd.handle()
async def _(event: MessageEvent):
    """从归档池里按语义找回记忆。归档是软删，向量还在，所以能捞回来。"""
    arg = _arg(event, "/memory_restore").strip()
    if not arg:
        await restore_cmd.finish("用法：/memory_restore 关键词（从归档池里按语义找回，最多 3 条）")
    try:
        vec = (await core.memory.embed([arg]))[0]
    except Exception as e:  # noqa: BLE001
        await restore_cmd.finish(f"取向量失败：{type(e).__name__}: {e}")
        return
    try:
        items = await asyncio.to_thread(core.memory.restore, vec, 3)
    except Exception as e:  # noqa: BLE001
        await restore_cmd.finish(f"恢复失败：{type(e).__name__}: {e}")
        return
    if not items:
        await restore_cmd.finish("归档池里没有足够相关的记忆（阈值同召回下限）")
    lines = [f"已恢复 {len(items)} 条："]
    for it in items:
        lines.append(f"- cos={it['score']} imp={it['importance']} {(it['text'] or '')[:60]}")
    await restore_cmd.finish(NL.join(lines))


@prompt_last_cmd.handle()
async def _(event: MessageEvent):
    """看最近一轮实际发给模型的 prompt。排障用：只留哈希等于没留。"""
    arg = _arg(event, "/prompt_last").strip()
    n = int(arg) if arg.isdigit() else 1
    rows = await core.last_prompts(n)
    if not rows:
        await prompt_last_cmd.finish("还没有 prompt 快照（要么刚重启，要么一条都没跑过）")
    blocks = []
    for r in rows:
        try:
            when = datetime.fromtimestamp(float(r["ts"] or 0)).strftime("%m-%d %H:%M:%S")
        except Exception:  # noqa: BLE001
            when = "?"
        head = f"[{when}] {r['session']} · {r['chars']} 字符 · {r['flags'] or '无标记'}"
        body = r["body"] or "（这条是加正文之前记的，只有指纹）"
        blocks.append(head + NL + body[:600] + ("…" if len(body) > 600 else ""))
    await prompt_last_cmd.finish((NL + NL).join(blocks)[:1800])


@mem_status_cmd.handle()
async def _():
    """记忆链路健康快照：哪一环静默失败了，先从这几个数看。"""
    from app_core import status as status_mod

    try:
        await mem_status_cmd.finish(status_mod.render(status_mod.snapshot()))
    except Exception as e:  # noqa: BLE001
        await mem_status_cmd.finish(f"取不到状态：{type(e).__name__}: {e}")


@conflicts_cmd.handle()
async def _():
    """看「被新事实推翻」的旧记忆。矛盾检测是软删，这里能看到它判了什么、为什么。"""
    items = core.memory.superseded(8)
    if not items:
        await conflicts_cmd.finish("最近没有「被新记忆取代」的旧记忆")
    lines = ["最近被取代的旧记忆（旧 → 新）："]
    for it in items:
        m = it.get("meta") or {}
        try:
            when = datetime.fromtimestamp(float(m.get("superseded_at") or 0)).strftime("%m-%d %H:%M")
        except Exception:  # noqa: BLE001
            when = "?"
        why = str(m.get("supersede_reason") or "")[:24]
        lines.append(f"· [{when}] {(it.get('text') or '')[:42]}…｜{why}")
    lines.append("判错了想捞回来：/memory_restore 关键词")
    await conflicts_cmd.finish(NL.join(lines))


def _page_arg(event: MessageEvent, cmd: str) -> tuple[str, str]:
    """取命令参数并解析成页面路径。返回 (原参数, 路径)；路径空 = 没找到。"""
    arg = _arg(event, cmd).strip()
    return arg, (wiki.resolve(arg) if arg else "")


@kb_verify.handle()
async def _(event: MessageEvent):
    """人工确认：这一页现在还成立。

    整条知识链里唯一的真值来源就是人。没有这一步，模型只能一直把自己以前
    写的东西当事实用，误差会随着时间复利。
    """
    arg, path = _page_arg(event, "/kb_verify")
    if not arg:
        await kb_verify.finish("用法：/kb_verify 标题 —— 人工确认这一页现在还成立")
    if not path:
        await kb_verify.finish(f"没找到这一页：{arg}，先 /note_list 看文件名")
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    ok = learn.set_meta(path, verified=now, status="active")
    wiki.set_verified(path, learn.parse_ts(now), "active")
    name = Path(path).stem
    await kb_verify.finish(f"已验证：{name}（{now}）" if ok else f"写入失败：{name}")


@kb_deprecate.handle()
async def _(event: MessageEvent):
    """标记废弃：不再进目录，但文件保留（历史和审计还要它）。"""
    arg, path = _page_arg(event, "/kb_deprecate")
    if not arg:
        await kb_deprecate.finish("用法：/kb_deprecate 标题 —— 标记这页已过时（文件保留，不再进目录）")
    if not path:
        await kb_deprecate.finish(f"没找到这一页：{arg}，先 /note_list 看文件名")
    ok = learn.set_meta(path, status="已废弃")
    wiki.set_verified(path, 0.0, "已废弃")
    name = Path(path).stem
    await kb_deprecate.finish(f"已标记废弃：{name}（文件保留，不再进索引）" if ok else f"写入失败：{name}")


@raw_cmd.handle()
async def _(event: MessageEvent):
    """手动归档某天的对话原文（默认昨天），补跑或回溯用。"""
    arg = _arg(event, "/raw_archive").strip()
    day = arg if len(arg) == 10 and arg.count("-") == 2 else ""
    msg = await asyncio.to_thread(learn.archive_raw, day)
    await raw_cmd.finish(msg)
