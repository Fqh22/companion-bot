"""工具循环：模型要调工具时就跑它，最多 N 轮。

从 chat.py 抽出来的原因：这是唯一一段「进 messages + 工具表，出文本」的纯流程，
也是失败面最大、改得最勤的地方。抽出来以后能脱离 ChatCore 单测
超时 / 重复调用 / 轮数用尽三条路径。

依赖全部由参数注入：complete / stream 是 LLM 调用，tools_mod 是工具模块。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

log = logging.getLogger("qqbot.tool_loop")

# 只读工具可以在一轮里并发跑；写操作必须串行（同名文件别互相覆盖）。
# 名单放在 tools 模块里，这里只做兜底。
PER_TURN_CAP = {"meme_send": 1}

_DEFAULT_READ_ONLY = frozenset({
    "web_search", "web_open", "kb_read", "kb_search", "memory_search", "memory_read",
    "diary_read", "skill_read", "skill_list", "note_list", "remind_list",
})
# 并发时情绪事件可能同时落盘，串起来写（state.json 不是并发安全的）
_mood_lock = asyncio.Lock()


async def _mood(core, kind: str) -> None:
    """让 core 记一笔情绪事件。core 在测试里常是桩，所以什么都吞。"""
    fn = getattr(core, "mood_event", None)
    if fn is None:
        return
    try:
        async with _mood_lock:
            await fn(kind)
    except Exception:  # noqa: BLE001
        pass


async def _exec_one(core, exec_fn, name: str, args: dict, images: list[str],
                    tool_timeout: float) -> str:
    """执行单个工具，把超时/异常都变成「喂回模型的文字」。"""
    t0 = time.perf_counter()
    try:
        result = await asyncio.wait_for(exec_fn(core, name, args, images),
                                        timeout=tool_timeout)
    except asyncio.TimeoutError:
        result = (f"工具 {name} 超时（>{tool_timeout:.0f}s），"
                  "换个办法或者直接回答")
        log.warning("工具超时：%s", name)
        await _mood(core, "tool_fail")
    except Exception as e:  # noqa: BLE001
        # 单个工具炸了不该让整轮回复失败：把错误原样喂回模型，它还能换个办法
        result = f"工具 {name} 出错了：{type(e).__name__}: {e}"
        log.warning("工具 %s 异常：%s", name, e)
        await _mood(core, "tool_fail")
    log.info("工具 %s %.0fms 结果 %d 字", name,
             (time.perf_counter() - t0) * 1000, len(result or ""))
    return result or ""


async def run(core, msgs: list[dict], *,
              complete, stream, tools_mod, tools_banned: set[str],
              max_rounds: int, tool_timeout: float, total_timeout: float,
              images: list[str] | None = None,
              on_text=None, stats: dict | None = None) -> str:
    """跑工具循环，返回最终正文（可能为空串，表示模型没给出正文）。

    complete(msgs, tools_arg) -> dict
    stream(msgs, tools_arg, on_text) -> dict
    stats：可选计数器，会填 rounds（跑了几轮 LLM）和 tools（执行了几次工具），
    调用方用它算「这一轮有多费劲」——查三次资料再写长答案，比寒暄累得多。
    """
    st = stats if stats is not None else {}
    st.setdefault("rounds", 0)
    st.setdefault("tools", 0)
    images = images if images is not None else []
    tools_arg = [t for t in tools_mod.TOOLS
                 if (t.get("function") or {}).get("name") not in tools_banned]
    work = [dict(m) for m in msgs]
    first = True
    done: dict[str, str] = {}
    started = time.perf_counter()

    for _ in range(max_rounds):
        if time.perf_counter() - started > total_timeout:
            log.warning("工具总预算 %.0fs 用尽，提前收尾", total_timeout)
            break
        if first and on_text:
            # 第一轮流式：首字能早 1 秒左右出来。若这轮其实要调工具，
            # 正文通常为空，on_text 不会被触发，不影响后续。
            msg = await stream(work, tools_arg, on_text)
        else:
            msg = await complete(work, tools_arg)
        first = False
        st["rounds"] += 1
        calls = msg.get("tool_calls") or []
        if not calls:
            return (msg.get("content") or "").strip()
        work.append({"role": "assistant",
                     "content": msg.get("content") or "",
                     "tool_calls": calls})
        # 官方要求：带 tools 的请求必须把每轮 reasoning_content 原样回传，
        # 否则后续请求可能 400。关闭思考时该字段为空，不用带。
        if msg.get("reasoning_content"):
            work[-1]["reasoning_content"] = msg["reasoning_content"]
        read_only = getattr(tools_mod, "READ_ONLY_TOOLS", _DEFAULT_READ_ONLY)
        results: list[str] = [""] * len(calls)
        pending: list[tuple[int, str, dict, str]] = []
        seen: set[str] = set()
        for i, tc in enumerate(calls):
            fn = (tc.get("function") or {})
            name = fn.get("name") or ""
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except Exception:
                args = {}
            if not isinstance(args, dict):
                args = {}
            key = name + "|" + json.dumps(args, sort_keys=True, ensure_ascii=False)[:200]
            if key in done:
                results[i] = done[key] + "\n（重复调用已跳过：同样的参数刚执行过）"
                log.info("工具 %s 重复调用被跳过", name)
            elif key in seen:
                results[i] = "（同样的参数这一轮已经调用过，结果见上一条）"
                log.info("工具 %s 本轮重复，已跳过", name)
            elif name in PER_TURN_CAP and st.get("per_tool", {}).get(name, 0) >= PER_TURN_CAP[name]:
                results[i] = "这一轮已经发过表情包了，不要再发"
                log.info("工具 %s 达到每轮上限，已跳过", name)
            else:
                seen.add(key)
                pending.append((i, name, args, key))
                if name in PER_TURN_CAP:
                    st.setdefault("per_tool", {})
                    st["per_tool"][name] = st["per_tool"].get(name, 0) + 1

        # 一轮里的多个工具并发跑：只读的并行，写操作的串行。
        # 实测一轮 5 个工具串行要 12 秒（两次搜索 + 两次开网页），并行后约 3 秒。
        par = [p for p in pending if p[1] in read_only]
        seq = [p for p in pending if p[1] not in read_only]
        if par:
            outs = await asyncio.gather(*[
                _exec_one(core, tools_mod.execute, name, args, images, tool_timeout)
                for _, name, args, _ in par])
            for (i, _, _, key), out in zip(par, outs):
                results[i] = out
                done[key] = out
                st["tools"] += 1
        for i, name, args, key in seq:
            out = await _exec_one(core, tools_mod.execute, name, args, images,
                                  tool_timeout)
            results[i] = out
            done[key] = out
            st["tools"] += 1

        for i, tc in enumerate(calls):
            work.append({"role": "tool", "tool_call_id": tc.get("id", ""),
                         "content": results[i]})
    # 轮数用尽时模型可能还在调工具。这时去掉 tools 再问一次，逼它给正文，
    # 否则用户会收到一句莫名其妙的兜底「在」。
    try:
        final = await complete(work, None)
        return (final.get("content") or "").strip()
    except Exception:
        return ""
