"""只读命令的文本生成：QQ 插件和 console 共用。

这些命令做的事（查库、格式化）和 NoneBot 无关；写在插件里，换通道就得重写。
现在两边都调这里，输出保持一致。有副作用的命令（清理、恢复、审批）暂时留在插件层。
"""
from __future__ import annotations

from app_core import status

NL = chr(10)


def ping_text(core) -> str:
    """pong + 配置状态。admin 没配时记忆不写、主动消息不发，但服务看着正常。"""
    issues = core.config_issues()
    if issues:
        return "pong" + NL + "⚠️ " + (NL + "⚠️ ").join(issues)
    return f"pong（{core.config_status()}）"


def mood_text() -> str:
    from app_core import mood

    s = mood.settle(mood.load())
    mood.save(s)
    why = [x.get("text") for x in (s.get("why") or [])[-2:] if x.get("text")]
    text = (f"心情 {s['valence']:+.2f}（这阵子 {s.get('valence_base', 0.0):+.2f}） / "
            f"精力 {s['energy']:.2f} / 互动 {s['social']:.2f}")
    if why:
        text += NL + "最近：" + "；".join(why)
    return text


async def usage_text(core) -> str:
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
    ]
    for model, calls, pt, ct, cost in d7["models"]:
        lines.append(f"  {model}: {calls} 次, in {pt or 0} / out {ct or 0} ≈ ¥{cost}")
    return NL.join(lines)


def mem_status_text() -> str:
    return status.render(status.snapshot())


def profile_text(learn) -> str:
    """画像：每个字段的置信度、多久没被确认、会不会被注入。"""
    from app_core import profile as profile_mod

    fields = learn.profile_fields()
    if not fields:
        return "现在没有画像（或者全部过期了）"
    conf = learn.profile_conf()
    seen = learn.profile_seen()
    prev = learn.profile_previous()
    lines = ["现在的画像（内容｜置信度｜最后一次有证据）"]
    for k, v in fields.items():
        d = profile_mod.age_days(seen.get(k))
        age = "刚确认" if d is None else (f"{d:.0f} 天前" if d >= 1 else "今天")
        eff = profile_mod.effective_conf(float(conf.get(k, 0.7)), d)
        flag = "" if eff >= profile_mod.MIN_CONF else "（已过期，不注入）"
        lines.append(f"· {k}：{v}｜{conf.get(k, 0.7):.2f}｜{age}{flag}")
        p = prev.get(k) or {}
        if p.get("text"):
            pd = profile_mod.age_days(p.get("at"))
            when = "最近" if (pd is None or pd < 1) else f"{pd:.0f} 天前"
            lines.append(f"    ↳ 上次：{p['text']}（{when}改的）")
    lines.append("清一个：/profile_forget 字段名｜全清：/profile_clear")
    return NL.join(lines)
