"""对话事实来源和顺序核对，使用隔离数据库与合成输入。"""
from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import patch

from harness import bootstrap, check, report, temp_root

bootstrap()
from companion_core import EngineConfig, create_application


async def main() -> int:
    app = create_application(EngineConfig(temp_root("grounding-")))
    await app.start()
    core = app.core
    core.admin_uid = "owner"

    from app_core import followup, grounding, prompt, summarize
    from app_core.chat_turn_base import Turn

    now = time.time()
    session = "private:owner"
    image = "[发了图片]（附了 1 张图）｜图注：视频作者选了24351，猜的是ISFJ。"
    options = "1.随心自在\n2.阖家幸福\n3.事业成就\n4.长久爱意\n5.精神世界"
    history = [
        {"role": "user", "content": image, "ts": now - 1600},
        {"role": "user", "content": "那我说一下我的排序你能猜出来我什么MBTI吗", "ts": now - 1500},
        {"role": "user", "content": "15243", "ts": now - 220},
        {"role": "assistant", "content": "5 被你压在最末，因为你说这个选项虚。", "ts": now - 200},
        {"role": "user", "content": options, "ts": now - 170},
        {"role": "user", "content": "那个图片是别人的视频，不是我的排序", "ts": now - 30},
    ]

    def read(messages):
        return next((json.loads(m["content"].split("\n", 1)[1]) for m in messages
                     if m["content"].startswith("[本轮引用与核对资料")), {})

    turn = Turn(session, "15243", [], None, True, "fixture", time.perf_counter(), history=history)
    await core._turn_compose(turn)
    facts = read(turn.msgs)
    check("回放进入正常模型入口，原话仍是末条", turn.msgs[-1] == {"role": "user", "content": "15243"})
    check("5 是第二，3 是最后，完整选项按名次排列",
          facts.get("已核对名次", {}).get("5") == 2 and facts.get("已核对名次", {}).get("3") == 5
          and facts.get("用户给的选项按排名") == ["随心自在", "精神世界", "阖家幸福", "长久爱意", "事业成就"])
    check("澄清归属保留原排序，截图的数字与助手猜测不进入核对结果",
          facts.get("用户输入的排序") == "15243" and "别人的视频" in facts.get("用户最新明确的纠正", "")
          and "24351" not in str(facts) and "选项虚" not in str(facts))
    check("追问沿用排序", read(grounding.evidence(history, "那像什么", now=now)).get("用户输入的排序") == "15243")
    check("单独数字、验证码与重复编号不猜排名",
          not read(grounding.evidence([], "15243", now=now))
          and not read(grounding.evidence(history, "验证码：15243", now=now))
          and "已核对名次" not in read(grounding.evidence(history, "15143", now=now)))
    check("没有选项表时只核对数字，不猜标签",
          "用户给的选项按排名" not in read(grounding.evidence(history[:2], "15243", now=now)))
    different = history + [{"role": "user", "content": "换个话题，新题的排序是什么", "ts": now}]
    check("新话题切断旧排序", not read(grounding.evidence(different, "那呢", now=now)))
    changed = history + [{"role": "user", "content": options.replace("精神世界", "成长空间"), "ts": now}]
    check("选项被另一张表替换后重新确认顺序",
          "用户输入的排序" not in read(grounding.evidence(changed, "那像什么", now=now)))
    quoted = read(grounding.evidence([], "你怎么看", quote="对方：我已经签完了", now=now))
    check("平台引用作为有标注的资料进入回复", quoted.get("平台引用内容（发言人未核实）") == "对方：我已经签完了")
    check("旧图注与发言区分，摘要区分转述",
          "机器人生成的图注" in prompt.dated_history(history)[0]["content"]
          and "[他转述]" in summarize.format_dialog([("user", "截图里说：已经签完了")]))
    dated = prompt.dated_history([{"role": "user", "content": "明天再看｜图注：明天交稿", "ts": now}])
    check("图内的相对日期沿用原文，用户自己的时间照常按消息锚定",
          "明天再看" not in dated[0]["content"] and "明天交稿" in dated[0]["content"])
    check("猜我的选择不自动搜索，明确要求查证时仍搜索",
          not core._wants_search(turn, "你猜猜我会怎么选") and core._wants_search(turn, "帮我查一下最新的价格"))
    with patch.object(followup, "control_turn") as control, patch.object(followup, "answer_turn") as answer:
        core._prepare_followup(turn, "截图里说：短租不用跟进了")
        core._prepare_followup(turn, "[发了图片]｜图注：短租已经签好了")
        core._prepare_followup(turn, "引用上一句：短租不用跟进了")
        check("引用或图注无法冒充用户执行跟进变更", not control.called and not answer.called)

    revoked = []

    class Port:
        async def supersede(self, ref, *args):
            revoked.append(ref)
            return True

    turn.injected_insights = ["a", "b"]
    turn.memories = [{"doc_id": "a", "text": "偏爱热闹"}, {"doc_id": "b", "text": "喜欢浓茶"}]
    turn.stored = "不对，我不是偏爱热闹"
    with patch.object(core, "_memory_port", return_value=Port()):
        await core._commit_memory(turn)
    check("纠正只推翻明确点名的洞察", revoked == ["a"], revoked)

    row = await core.remember(session, "user", "[发了图片]")
    writes = await asyncio.gather(core.append_to_message(row, "｜图注：第一份"),
                                  core.append_to_message(row, "｜图注：另一份"))
    saved = (await core.history(session, max_items=1))[0]["content"]
    check("两路并发图注只写一份", sum(writes) == 1 and saved.count("｜图注：") == 1)
    await app.stop()
    return report()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
