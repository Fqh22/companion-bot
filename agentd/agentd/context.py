"""上下文组装与压缩（§十二）。

原则：工具原文不进上下文——事件里只留「结论 + 文件路径」，所以压缩输入天然很小。
错误事件永不参与合并（否则恢复后不知道当初为什么失败）。
"""
from __future__ import annotations

import json

from .config import Config
from .llm import LLM
from .store import Store

SYSTEM = """你是一个执行型 agent 的一部分。你在一个隔离的工作区里完成一个具体步骤。

规则：
1. 一次只调一个工具，不要空谈。用 JSON 回答，格式：
   {"tool":"工具名","args":{...}}
2. 命令失败就换办法，不要重复同样的命令。
3. 文件很大时先用 grep/count/tail 拿统计，不要整文件读。
4. 这一步做完了就调 finish：{"tool":"finish","args":{"summary":"一句话结论"}}
5. 不要编造执行结果。只能根据工具返回的内容下结论。
6. **抓网页 / 搜索必须用 fetch / search 工具**。shell 里没有外网（只有包源白名单），
   写 urllib/requests/curl 一定失败。

{tools}
"""


class Context:
    def __init__(self, cfg: Config, store: Store, llm: LLM, tools_doc: str,
                 work=None) -> None:
        self.cfg = cfg
        self.store = store
        self.llm = llm
        self.system = SYSTEM.replace("{tools}", tools_doc)
        if work is None:
            from .config import WORK as _work
            work = _work
        self.work = work

    # ---------- digest ----------
    def digest(self, task: dict) -> str:
        try:
            extra = json.loads(task.get("extra") or "{}")
        except Exception:  # noqa: BLE001
            extra = {}
        return str(extra.get("digest") or "")

    def set_digest(self, task: dict, text: str) -> None:
        try:
            extra = json.loads(task.get("extra") or "{}")
        except Exception:  # noqa: BLE001
            extra = {}
        extra["digest"] = text[:self.cfg.digest_max_chars]
        self.store.set_task(task["id"], extra=json.dumps(extra, ensure_ascii=False))

    # ---------- 组装 ----------
    def build(self, task: dict, steps: list[dict], step: dict,
              recent: list[dict]) -> list[dict]:
        plan = "\n".join(
            f"{s['idx']}. [{s['status']}] {s['desc']}" for s in steps)
        mem = "\n".join(
            f"- ({e['kind']}) {e['payload'][:200]}" for e in recent[-3:])
        errors = [e for e in recent if e["kind"] == "error"][-5:]
        err_block = "\n".join(f"- {e['payload'][:300]}" for e in errors)
        answers = [e for e in recent if e["kind"] == "answer"][-2:]
        ans_block = "\n".join(f"- {e['payload'][:300]}" for e in answers)
        state = self.project_state(task)

        user = (
            f"【目标】{task['goal']}\n\n"
            f"【工作目录】/work/{task['id']}（命令默认在这里执行，**用相对路径**）\n\n"
            + (f"【这个项目之前做过】\n{state}\n\n" if state else "")
            + f"【计划】\n{plan}\n\n"
            f"【已知摘要】{self.digest(task) or '（还没有）'}\n\n"
            f"【最近动作】\n{mem or '（还没有）'}\n"
            + (f"\n【我刚回答过】\n{ans_block}\n" if ans_block else "")
            + (f"\n【失败过的事（不要重复）】\n{err_block}\n" if err_block else "")
            + f"\n【现在要做】第 {step['idx']} 步：{step['desc']}\n"
            f"验收条件：{json.dumps(step.get('verify') or {}, ensure_ascii=False)}\n"
            "只输出一个 JSON 工具调用。"
        )
        return [{"role": "system", "content": self.system},
                {"role": "user", "content": user}]

    def project_state(self, task: dict, limit: int = 8) -> str:
        """项目最近做过什么（跨任务状态）。没有就返回空串。"""
        project = (task.get("project") or "default").replace("/", "_")[:40]
        p = self.work / "projects" / project / "STATE.md"
        try:
            lines = [ln.strip() for ln in p.read_text(encoding="utf-8").splitlines()
                     if ln.startswith("- ")]
            return "\n".join(lines[-limit:])
        except Exception:  # noqa: BLE001
            return ""

    # ---------- 压缩 ----------
    async def compact(self, task: dict, steps: list[dict]) -> None:
        done = [s for s in steps if s["status"] == "done" and s.get("result")]
        if len(done) < self.cfg.compact_every:
            return
        chunk = done[-self.cfg.compact_every:]
        old = self.digest(task)
        payload = "\n".join(f"- {s['desc']} → {str(s['result'])[:200]}" for s in chunk)
        prompt = (
            f"把下面的执行记录压成不超过 {self.cfg.digest_max_chars} 字的摘要，"
            f"保留关键数字、文件名和失败原因，不要评论：\n\n"
            f"【已有摘要】{old or '（空）'}\n【新增记录】\n{payload}")
        text = await self.llm.chat(
            [{"role": "user", "content": prompt}], "compact", task_id=task["id"],
            max_tokens=300)
        if text.strip():
            self.set_digest(task, text.strip())
            self.store.add_event(task["id"], "compact", text.strip()[:300])
