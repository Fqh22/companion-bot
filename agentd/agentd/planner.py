"""计划器（§六）：首轮出 3–8 步，只在失败/推翻假设时重规划。

noop 比例超过 40% 先让模型重写一次（§7.1）——planner 偷懒要当场拦住。
"""
from __future__ import annotations

import json
from pathlib import Path

from .config import Config
from .llm import LLM, extract_json
from .plan import validate_plan
from .store import Store

PLAN_SYSTEM = """你是一个执行型 agent 的规划器。把目标拆成 3–8 个可独立验证的步骤。

工具契约（直接决定 verify 该写什么，别写错）：
- fetch  / search  只**返回文字**，不生成文件。要落盘必须另写一步 write。
- write            生成文件 → verify 用 {"file_exists": ...}
- shell            可以生成文件、跑命令 → verify 用 {"exit_code":0} 或 file_exists
- 抓网页/搜索**只能**用 fetch / search；shell 里没有外网

要求：
1. 每步一句话，具体到能直接执行（不要写“分析一下”这种空话）。
2. 每步必须带验收条件 verify，从这些里选：
   - {"exit_code":0}
   - {"file_exists":"x.md","min_size":100}
   - {"contains":"关键词"}
   - {"regex":"\\\\d+ 条"}
   - {"custom":"python check.py"}    （自定义脚本，退出码 0 为通过）
     custom 只能引用**前面步骤已经创建**的脚本；没有创建就用 file_exists / contains，
     不要凭空写 check.py。
   尽量避免 {"noop":true}——只有在确实无法机械校验时才用，且必须再带一个 custom。
3. 声明依赖：depends_on 里放前置步骤的 idx。
4. 只输出 JSON：{"steps":[{"idx":0,"desc":"...","depends_on":[],"verify":{...}}]}
"""


class Planner:
    def __init__(self, cfg: Config, store: Store, llm: LLM) -> None:
        self.cfg = cfg
        self.store = store
        self.llm = llm

    async def plan(self, task: dict) -> list[dict]:
        steps = await self._ask(task, hint="")
        if noop_ratio(steps) > 0.4:
            self.store.add_event(task["id"], "note", "计划 noop 比例过高，要求重写")
            steps2 = await self._ask(task, hint=(
                "上一次的计划里有太多无法验证的步骤。给每一步补上可执行的自定义检查"
                "（{\"custom\": \"python check.py\"}），不要用 noop。"))
            if steps2:
                steps = steps2
        return steps or [{"idx": 0, "desc": task["goal"][:80], "depends_on": [],
                          "verify": {"noop": True}}]

    async def _ask(self, task: dict, hint: str) -> list[dict]:
        # 用户发来的文件已经搬进工作区，得让规划器知道，否则它会去别处找
        files: list[str] = []
        ctx_text = ""
        try:
            extra = json.loads(task.get("extra") or "{}")
            files = extra.get("files") or []
            files = [Path(str(p)).name for p in files]
            # 派活时机器人挑出来的「相关记忆」，2-3 条，不是整段聊天记录
            ctx_text = str(extra.get("context") or "").strip()
        except Exception:  # noqa: BLE001
            files, ctx_text = [], ""
        fline = (f"【已有文件】{('、'.join(files))}（在工作区根目录，用 read/list 就能看到）\n"
                 if files else "")
        # cline 之前只在 f-string 里出现、从没赋值：这条链路一跑就 NameError，
        # /task 的规划整条挂掉。补上真正的背景块。
        cline = (f"【背景】（聊到这件事时他说过的话，可能相关，不一定对）\n"
                 f"{ctx_text[:400]}\n" if ctx_text else "")
        prompt = (f"【目标】{task['goal']}\n"
                  f"【环境】工作区是 /work/{task['id']}；沙箱里只有 python 和常用命令，\n"
                  f"  而且**不能直接访问外网**（只有包源白名单）。\n"
                  f"【重要】抓网页 / 搜索必须用工具 fetch / search，\n"
                  f"  不要在 shell 里写 urllib、requests、curl——那一定拿不到数据。\n"
                  f"{fline}{cline}"
                  + (f"【修正要求】{hint}\n" if hint else ""))
        text = await self.llm.chat(
            [{"role": "system", "content": PLAN_SYSTEM},
             {"role": "user", "content": prompt}],
            "plan", task_id=task["id"], max_tokens=900)
        data = extract_json(text)
        raw = data.get("steps") or []
        out: list[dict] = []
        for i, s in enumerate(raw[:8]):
            if not isinstance(s, dict):
                continue
            out.append({
                "idx": int(s.get("idx", i)),
                "desc": str(s.get("desc") or "")[:200],
                "depends_on": [int(x) for x in (s.get("depends_on") or [])],
                "verify": s.get("verify") if isinstance(s.get("verify"), dict) else {},
            })
        return validate_plan([s for s in out if s["desc"]])


def sanitize_steps(steps: list[dict]) -> list[dict]:
    """兼容旧测试名；实际逻辑在 plan.validate_plan。"""
    return validate_plan(steps)


def noop_ratio(steps: list[dict]) -> float:
    if not steps:
        return 0.0
    n = sum(1 for s in steps if (s.get("verify") or {}).get("noop"))
    return n / len(steps)


def plan_json(steps: list[dict]) -> str:
    return json.dumps(steps, ensure_ascii=False)
