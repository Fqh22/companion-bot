#!/usr/bin/env python3
"""核算最坏情况下每轮对话的 prompt 规模与成本。

诊断脚本（云端 ad-hoc）：会真实调一次 DeepSeek（max_tokens=1 只为拿 usage），
不写库、不发消息。用法：/opt/nonebot/venv/bin/python ops/probe-budget.py
"""
import asyncio
import json
import sys

import httpx

sys.path.insert(0, "/opt/nonebot")

from app_core.chat import ChatCore, deepseek_key  # noqa: E402

core = ChatCore()

persona = core.persona
sys_prompt = persona.get("system_prompt", "")
dialogs = persona.get("begin_dialogs") or []

print("人格 system_prompt:", len(sys_prompt), "字")
print("few-shot 组数:", len(dialogs) // 2, " 总长:", sum(len(d) for d in dialogs), "字")

# 按 build_messages 的各项上限构造满载
ctx = core.history_max_chars          # 从 config 读，改了配置重跑就是准的
mood_text = "[你此刻的状态] 心情偏暖（+0.30），精力中（0.70），今天聊得一般。调整语气和长度：正常聊。不要说出这些数值，也别解释状态。"
mem_block = "\n".join("- " + ("记忆内容" * 55) for _ in range(5))          # 5 × 220
kb_block = "\n\n".join("知识库正文" * 100 for _ in range(3))                 # 3 × 500
skill_block = "\n\n".join("技能步骤" * 150 for _ in range(2))               # 2 × 600

# 让历史正好用满 history_max_chars
n_hist = 20
per = ctx // n_hist
history = [{"role": "user" if i % 2 == 0 else "assistant",
            "content": ("历史对话内容" * 100)[:per]} for i in range(n_hist)]

msgs = [{"role": "system", "content": sys_prompt},
        {"role": "system", "content": mood_text},
        {"role": "system", "content": f"[你记得的事]\n{mem_block}"},
        {"role": "system", "content": f"[你的学习笔记]\n{kb_block}"},
        {"role": "system", "content": f"[你以前总结的做法]\n{skill_block}"}]
for i in range(0, len(dialogs) - 1, 2):
    msgs.append({"role": "user", "content": dialogs[i]})
    msgs.append({"role": "assistant", "content": dialogs[i + 1]})
msgs.extend(history)
msgs.append({"role": "user", "content": "他刚发来的一句话"})

total_chars = sum(len(m["content"]) for m in msgs)
print(f"\n满载消息 {len(msgs)} 条，总 {total_chars} 字")
for m in msgs[:5]:
    print(f"  [{m['role']}] {len(m['content'])} 字")
print(f"  ... 历史 {len(history)} 条共 {sum(len(m['content']) for m in history)} 字")


async def main() -> None:
    payload = {"model": "deepseek-flash", "messages": msgs,
               "max_tokens": 1, "thinking": {"type": "disabled"}}
    async with httpx.AsyncClient(timeout=90) as c:
        r = await c.post("https://api.deepseek.com/v1/chat/completions",
                         headers={"Authorization": "Bearer " + deepseek_key()},
                         json=payload)
        d = r.json()
    u = d.get("usage") or {}
    pt = u.get("prompt_tokens", 0)
    print(f"\n实际 prompt_tokens: {pt}")
    print(f"换算: {total_chars} 字 ≈ {pt} tokens（约 {total_chars / max(pt,1):.1f} 字/token）")

    # 按高峰价 2 元/百万输入
    print(f"单轮输入成本: {pt / 1e6 * 2:.5f} 元")
    print(f"若一次回复跑 3 轮工具: {pt * 4 / 1e6 * 2:.5f} 元")
    print(f"若一次回复跑 6 轮工具: {pt * 7 / 1e6 * 2:.5f} 元")
    print(f"每天 100 轮普通对话(各 1 次调用): {pt * 100 / 1e6 * 2:.3f} 元")


asyncio.run(main())
