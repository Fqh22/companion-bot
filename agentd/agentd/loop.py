"""执行循环（§五）+ 队列（§5.1）+ 恢复（§8）+ 错误处理（§九）。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shlex
import shutil
import time
from pathlib import Path

from . import context as ctx_mod
from . import planner as planner_mod
from .config import Config
from .llm import LLM
from .sandbox import Sandbox
from .store import Store
from .tools import Tools, parse_action
from .verify import Observation, evaluate

TERMINAL = ("done", "blocked", "skipped")
_ATTACH_HINT = re.compile(
    r"(这份|收到的|附件|上传的|发来的|读取.{0,12}(pdf|docx?|xlsx?|pptx?)|"
    r"(pdf|docx?|xlsx?|pptx?).{0,12}(改|重排|提取|删除))", re.I)


def attachment_hint(goal: str) -> bool:
    """目标像是在引用一个附件，但任务没带文件时先问，不要空跑。"""
    return bool(_ATTACH_HINT.search(goal or ""))
BLOCKED_HINTS = ("Permission denied", "No such file", "not found", "路径越界",
                 "command not found", "cannot open")
# 需要人工点头的命令（§十三 审批）。宁可多拦，不可漏放。
DANGER = ("rm -rf", "rm -fr", "mkfs", "dd if=", "dd of=", "shutdown", "reboot",
          ":(){", "chmod 777", "> /etc/", "curl -x post", "truncate -s 0")


class Runner:
    def __init__(self, cfg: Config, store: Store, llm: LLM, sandbox: Sandbox,
                 tools: Tools, ctx: ctx_mod.Context,
                 planner: planner_mod.Planner, work: Path) -> None:
        self.cfg = cfg
        self.store = store
        self.llm = llm
        self.sandbox = sandbox
        self.tools = tools
        self.ctx = ctx
        self.planner = planner
        self.work = work
        self._msg: dict[int, dict] = {}     # task_id -> {ms, hb, last}
        self._stop = False

    # ---------- 主循环 ----------
    async def run_forever(self) -> None:
        last_cleanup = 0.0
        while not self._stop:
            try:
                task = self.store.running_task() or self.store.next_pending_task()
                if task:
                    await self.run_task(task)
                else:
                    await self.sandbox.maybe_stop()
                    if time.time() - last_cleanup > 3600:      # 每小时清一次
                        last_cleanup = time.time()
                        r = self.store.cleanup_workspace(self.work)
                        trimmed = self.store.outbox_trim()
                        expired = self.store.expire_waiting(24)
                        if r["logs"] or r["archived"] or trimmed or expired:
                            self.store.add_event(
                                0, "note",
                                f"清理：{r} outbox -{trimmed} 超时关闭 {expired}")
            except Exception as e:  # noqa: BLE001
                self.store.add_event(0, "error", f"主循环异常：{type(e).__name__}: {e}")
                await asyncio.sleep(2)
            await asyncio.sleep(1)

    def stop(self) -> None:
        self._stop = True

    # ---------- 单个任务 ----------
    async def run_task(self, task: dict) -> None:
        tid = task["id"]
        if task["status"] == "pending":
            self.store.set_task(tid, status="running", started_at=time.time())
            task = self.store.task(tid) or task
            self.store.add_event(tid, "note", f"开始：{task['goal'][:120]}")
            steps = self.store.steps(tid)
            if not steps:
                # 先把用户发来的文件搬进工作区，规划时才知道有什么可用
                try:
                    expected = json.loads(task.get("extra") or "{}").get("files") or []
                except Exception:  # noqa: BLE001
                    expected = []
                n_in = self._import_files(tid)
                if n_in:
                    self.store.add_event(tid, "note", f"收到 {n_in} 个文件")
                if expected and n_in == 0:
                    self._need_input(
                        tid, "附件没有搬进工作区，先别规划。请重新发文件再 /task。")
                    return
                if not expected and attachment_hint(task.get("goal") or ""):
                    self._need_input(
                        tid, "目标看起来需要附件，但这次没收到文件。"
                             "先发文件，再 /task；或直接说「不用附件」。")
                    return
                steps = await self.planner.plan(task)
                self.store.add_steps(tid, steps)
                steps = self.store.steps(tid)          # 重新取，带上 status/id
                self._write_plan(tid, steps)
                (self.work / str(tid)).mkdir(parents=True, exist_ok=True)
            # 不再发「收到，开始了」——任务不跑久就全程静默，只发结果
        else:
            steps = self.store.steps(tid)
            # 崩溃恢复：进程被杀时正在跑的步骤会停在 running，
            # 不重置的话它既不是 pending 也不是终态，永远轮不到它。
            stale = [s for s in steps if s["status"] == "running"]
            for s in stale:
                self.store.set_step(s["id"], status="pending")
                # 一步可能在「已产生副作用」和「写检查点」之间被杀。
                # 无法判断执行到哪，就明确提示模型先看现状，别盲目重做。
                acts = [e for e in self.store.events(tid, 50, ["act"])
                        if e.get("step_id") == s["id"]]
                if acts:
                    self.store.add_event(
                        tid, "error",
                        f"第 {s['idx']} 步可能已执行过（{acts[-1]['payload'][:80]}），"
                        "重跑前先确认现状", s["id"])
            if stale:
                self.store.add_event(tid, "note", f"恢复：重置 {len(stale)} 个中断步骤")
                steps = self.store.steps(tid)
            await self._reanchor(task, steps)

        await self._run_steps(task, steps)
        final = self.store.task(tid) or task
        await self._finish(final)

    # ---------- 步骤推进 ----------
    async def _run_steps(self, task: dict, steps: list[dict]) -> None:
        tid = task["id"]
        guard = 0
        while True:
            guard += 1
            if guard > self.cfg.max_steps * 4:      # 死循环保险
                self.store.set_task(tid, status="failed", finished_at=time.time(),
                                    report="循环次数异常，已强制停止")
                return
            task = self.store.task(tid) or task
            if task["status"] in ("cancelled", "paused", "done", "failed"):
                return
            if self._budget_hit(task):
                self.store.set_task(tid, status="failed", report=self._budget_msg(task),
                                    finished_at=time.time())
                return
            step = self._next_runnable(task, steps)
            if step is None:
                if await self._replan(task, steps):        # 卡住时给一次重规划机会
                    steps = self.store.steps(tid)
                    continue
                self._propagate_blocked(tid, steps)
                if all(s["status"] in TERMINAL for s in steps):
                    self.store.set_task(tid, status="done", finished_at=time.time())
                else:
                    self.store.set_task(tid, status="failed", finished_at=time.time(),
                                        report="没有可执行的步骤（依赖全部阻塞）")
                return
            # 单步整体限时：一次 LLM 卡住时，deadline 检查永远轮不到
            left = max(30.0, float(task.get("deadline") or 0) - time.time())
            try:
                await asyncio.wait_for(self._run_step(task, step, steps), timeout=left)
            except asyncio.TimeoutError:
                self.store.set_step(step["id"], status="blocked",
                                    error=f"单步超时（{int(left)}s）",
                                    finished_at=time.time())
            steps = self.store.steps(tid)      # 状态在库里，必须重新读
            await self._heartbeat(task)

    def _next_runnable(self, task: dict, steps: list[dict]) -> dict | None:
        for s in steps:
            if s["status"] != "pending":
                continue
            deps = [steps[i] for i in s["depends_on"] if 0 <= i < len(steps)]
            if any(d["status"] in ("blocked", "skipped") for d in deps):
                self.store.set_step(s["id"], status="skipped",
                                    error="依赖步骤未完成")
                s["status"] = "skipped"
                continue
            if all(d["status"] == "done" for d in deps):
                return s
        return None

    def _propagate_blocked(self, tid: int, steps: list[dict]) -> None:
        """剩下的 pending 依赖都不可用时，标 skipped，避免死循环。"""
        for s in steps:
            if s["status"] == "pending":
                self.store.set_step(s["id"], status="skipped", error="上游阻塞")

    # ---------- 单步执行 ----------
    async def _run_step(self, task: dict, step: dict, steps: list[dict]) -> None:
        tid, sid = task["id"], step["id"]
        self.store.set_step(sid, status="running", started_at=time.time())
        # custom 验收也是 shell，必须和普通命令过同一道审批闸。
        # 放在最前面：等批准后整步会重跑，避免主操作已经执行两遍。
        _custom = str((step.get("verify") or {}).get("custom") or "")
        if _custom and is_dangerous(_custom) and \
                not self.store.approved_cmd(tid, sid, _custom):
            aid = self.store.add_approval(tid, sid, _custom)
            self.store.set_step(sid, status="blocked", finished_at=time.time(),
                                error="等待审批（custom 验收）")
            self._notify(tid, f"验收脚本要跑危险命令，等你点头（/approve {aid}）：\n"
                              f"{_custom[:120]}")
            self.store.add_event(tid, "error",
                                 f"待审批 #{aid}（custom 验收）：{_custom[:120]}", sid)
            return
        attempts = 0
        while attempts < 3:
            attempts += 1
            self.store.set_step(sid, attempts=attempts)
            recent = self.store.events(tid, limit=12)
            msgs = self.ctx.build(task, steps, step, recent)
            try:
                text = await self.llm.chat(msgs, "decide", task_id=tid,
                                           step_id=sid, max_tokens=1400)
            except Exception as e:  # noqa: BLE001
                self.store.add_event(tid, "error", f"模型调用失败：{type(e).__name__}", sid)
                await asyncio.sleep(3)
                continue

            tool, args = parse_action(text)
            if not tool:
                self.store.add_event(tid, "error", f"输出不是工具调用：{text[:120]}", sid)
                continue

            # 同样的调用重复第三次，就明确喊停：模型在打转
            sig = f"{tool}:{json.dumps(args, ensure_ascii=False, sort_keys=True)[:200]}"
            same = [e for e in self.store.events(tid, 8, ["note"])
                    if e["payload"].startswith("call-sig")]
            if sum(1 for e in same if sig[:80] in e["payload"]) >= 2:
                self.store.add_event(
                    tid, "error",
                    "重复调用：同一个工具和参数已经试过两次，这一次必须换完全不同的做法", sid)

            # 危险命令要人工点头（§十三）
            cmd = str(args.get("cmd") or "")
            if tool == "shell" and is_dangerous(cmd) and \
                    not self.store.approved_cmd(tid, sid, cmd):
                aid = self.store.add_approval(tid, sid, cmd)
                self.store.set_step(sid, status="blocked", finished_at=time.time(),
                                    error="等待审批")
                self._notify(tid, f"要执行危险命令，等你点头（/approve {aid}）：\n{cmd[:120]}")
                self.store.add_event(tid, "error", f"待审批 #{aid}：{cmd[:120]}", sid)
                return

            obs, logpath = await self.tools.call(tid, tool, args)
            self.store.add_event(tid, "note", f"call-sig {sig[:80]}", sid)
            self.store.add_event(tid, "act", f"{tool} {json.dumps(args, ensure_ascii=False)[:200]}", sid)
            self.store.add_event(tid, "observe",
                                 (obs.stdout or obs.stderr or "")[:300]
                                 + (f"（完整：{logpath}）" if logpath else ""), sid)

            # 自定义验收脚本必须真的跑进沙箱（在宿主上跑 = 白送逃逸）
            _custom_result: tuple[int, str] = (1, "未执行")
            _spec = step["verify"] or {}
            if "custom" in _spec:
                _c, _ = await self.tools.call(
                    tid, "shell", {"cmd": str(_spec["custom"])})
                _custom_result = (_c.exit_code, (_c.stdout or _c.stderr or "")[:200])
                self.store.add_event(tid, "observe",
                                     f"custom 验收：{_custom_result[0]} {_custom_result[1][:120]}",
                                     sid)

            ok, why, unverified = evaluate(
                step["verify"], obs,
                runner=None if "custom" not in _spec else (lambda spec: _custom_result),
                resolver=lambda path: self._resolve_file(tid, path))
            cls = classify(obs, why, ok)
            self.store.add_event(tid, "verify" if ok else "error",
                                 ("通过" if ok else f"{cls}: {why}")[:200], sid)

            if ok:
                self.store.set_step(
                    sid, status="done", finished_at=time.time(),
                    result=(str(args.get("summary") or "").strip()
                            or (obs.stdout or "").strip()
                            or f"完成：{step['desc'][:40]}")[:300],
                    error="")
                if unverified:
                    self.store.add_event(tid, "note", "这一步没有可机械验证的条件", sid)
                await self.ctx.compact(task, self.store.steps(tid))
                self._checkpoint(task, step["idx"], steps)
                return

            if cls in ("blocked", "resource"):
                self.store.set_step(sid, status="blocked", finished_at=time.time(),
                                    error=f"{cls}: {why}"[:300])
                return
            if cls == "transient":
                await asyncio.sleep(2 * attempts)
                continue
            # logic：换办法继续重试
        self.store.set_step(sid, status="blocked", finished_at=time.time(),
                            error="连续 3 次未通过")

    # ---------- 检查点 ----------
    def _checkpoint(self, task: dict, step_idx: int, steps: list[dict]) -> None:
        tid = task["id"]
        d = self.work / str(tid)
        files = []
        if d.exists():
            for p in sorted(d.rglob("*")):
                if p.is_file() and ".logs" not in p.parts:
                    files.append({"path": str(p.relative_to(d)),
                                  "size": p.stat().st_size,
                                  "sha": _sha(p)})
        blob = {
            "task_id": tid, "step_idx": step_idx, "goal": task["goal"],
            "plan": [{"idx": s["idx"], "status": s["status"]} for s in steps],
            "workspace": {"cwd": str(d), "files": files[:200]},
            "context_digest": self.ctx.digest(task),
            "budget": {"tokens_used": (self.store.task(tid) or {}).get("tokens_used", 0)},
        }
        self.store.save_checkpoint(tid, step_idx, blob)

    # ---------- 恢复时重锚定（§8.3）----------
    async def _reanchor(self, task: dict, steps: list[dict]) -> None:
        cp = self.store.last_checkpoint(task["id"])
        if not cp:
            return
        changed = _changed_files(cp["blob"], self.work / str(task["id"]))
        if not changed:
            return
        self.store.add_event(task["id"], "note", f"文件变化：{', '.join(changed[:5])}")
        prompt = (f"任务目标：{task['goal']}\n旧摘要：{self.ctx.digest(task) or '（空）'}\n"
                  f"这些文件在暂停期间变了：{', '.join(changed[:10])}\n"
                  "用不超过 200 字写新的摘要，说明哪些旧结论不再成立、接下来要注意什么。")
        try:
            text = await self.llm.chat([{"role": "user", "content": prompt}],
                                       "reanchor", task_id=task["id"], max_tokens=300)
            if text.strip():
                self.ctx.set_digest(task, text.strip())
                self.store.add_event(task["id"], "note", "已重锚定")
        except Exception:  # noqa: BLE001
            self.store.add_event(task["id"], "error", "重锚定失败，沿用旧摘要")

    # ---------- 文件核对 ----------
    def _resolve_file(self, task_id: int, path: str) -> int | None:
        """把验收条件里的路径解析到任务目录下的真实文件，返回大小。

        兼容三种写法：`a.txt`、`/work/a.txt`、`/work/<id>/a.txt`——
        模型在沙箱里看到的是 `/work`，但每次任务其实只该动自己那个子目录。
        """
        base = (self.work / str(task_id)).resolve()
        p = str(path or "").lstrip("/")
        if p.startswith("work/"):
            p = p[len("work/"):]
            if p.startswith(f"{task_id}/"):
                p = p[len(str(task_id)) + 1:]
        full = (base / p).resolve()
        if full != base and not str(full).startswith(str(base) + os.sep):
            return None
        return full.stat().st_size if full.is_file() else None

    # ---------- 预算 ----------
    def _budget_hit(self, task: dict) -> bool:
        t = self.store.task(task["id"]) or task
        if int(t.get("tokens_used") or 0) >= self.cfg.max_tokens:
            return True
        if t.get("deadline") and time.time() > float(t["deadline"]):
            return True
        if self.store.tokens_today() >= self.cfg.daily_tokens:
            return True
        return False

    def _budget_msg(self, task: dict) -> str:
        t = self.store.task(task["id"]) or task
        return (f"预算用尽（tokens {t.get('tokens_used')}/{self.cfg.max_tokens}）")

    # ---------- 文件进出 ----------
    def _need_input(self, tid: int, text: str) -> None:
        self.store.set_task(tid, status="needs_input",
                            finished_at=time.time(), report=text)
        self.store.outbox_push(tid, text, "")
        self.store.add_event(tid, "note", text)

    def _import_files(self, tid: int) -> int:
        """把 inbox 里属于这个任务的文件搬进 /work/<id>/。

        QQ 发来的文件由插件先落到 inbox，任务创建时带上文件列表。
        搬进来之后模型就能用 read/list/shell 直接处理。
        """
        task = self.store.task(tid) or {}
        try:
            paths = json.loads(task.get("extra") or "{}").get("files") or []
        except Exception:  # noqa: BLE001
            paths = []
        if not paths:
            return 0
        dst = self.work / str(tid)
        dst.mkdir(parents=True, exist_ok=True)
        n = 0
        for p in paths:
            try:
                src = Path(str(p))
                if not src.is_file():
                    continue
                target = dst / src.name
                if src.resolve() != target.resolve():
                    shutil.copy2(src, target)
                n += 1
            except Exception as e:  # noqa: BLE001
                self.store.add_event(tid, "error", f"文件搬入失败：{p} {e}")
        return n

    def _collect_outputs(self, tid: int) -> list[str]:
        """任务结束后回传的文件：任务开始后**新出现或被改过**的文件。

        不按扩展名筛。改成看时间戳，是因为黑名单永远补不全，
        而「这次任务动过的」是客观事实。
        """
        task = self.store.task(tid) or {}
        started = float(task.get("started_at") or 0)
        out: list[str] = []
        try:
            for p in sorted((self.work / str(tid)).iterdir()):
                if not p.is_file() or p.name == "PLAN.md":
                    continue
                if p.stat().st_size > 20 * 1024 * 1024:
                    continue
                if started and p.stat().st_mtime < started - 1:
                    continue                      # 没动过（比如原件）
                out.append(str(p))
                if len(out) >= 3:
                    break
        except Exception:  # noqa: BLE001
            pass
        return out

    # ---------- 通道 ----------
    def _notify(self, task_id: int, text: str) -> None:
        """发一条消息。**结果才是主角，过程默认不出声。**

        规则（2026-11-05 按用户要求收紧）：
        - 任务 20 秒内结束 → 全程零过程消息，只发结果；
        - 超过 20 秒 → 一条「还在跑（k/N）」；
        - 超过 3 分钟 → 再补一条，之后不再打扰。
        需要他回答（needs_input）和审批不受这条限制——那是「非它不可」的情况。
        """
        st = self._msg.setdefault(task_id, {"msg": 0, "last": 0.0})
        if st["msg"] >= 2:
            return
        st["msg"] += 1
        st["last"] = time.time()
        self.store.outbox_push(task_id, text)

    async def _heartbeat(self, task: dict) -> None:
        """长任务的进度提示：最多两条，且只在真的跑久了之后。"""
        tid = task["id"]
        st = self._msg.setdefault(tid, {"msg": 0, "last": 0.0})
        started = float((self.store.task(tid) or task).get("started_at") or 0)
        elapsed = time.time() - started if started else 0
        if st["msg"] == 0 and elapsed >= self.cfg.ack_after_seconds:
            steps = self.store.steps(tid)
            done = sum(1 for s in steps if s["status"] == "done")
            self._notify(tid, f"还在跑（{done}/{len(steps)} 步），好了直接发结果")
        elif st["msg"] == 1 and elapsed >= self.cfg.progress_after_seconds:
            steps = self.store.steps(tid)
            done = sum(1 for s in steps if s["status"] == "done")
            self._notify(tid, f"还在跑（{done}/{len(steps)} 步）")

    # ---------- 收尾 ----------
    async def _finish(self, task: dict) -> None:
        tid = task["id"]
        cur = self.store.task(tid) or task
        if cur["status"] in ("cancelled", "paused", "failed"):
            return                              # 已定性（预算/超时）不许被收尾覆盖
        steps = self.store.steps(tid)
        done = [s for s in steps if s["status"] == "done"]
        blocked = [s for s in steps if s["status"] == "blocked"]
        skipped = [s for s in steps if s["status"] == "skipped"]
        report = (f"完成 {len(done)}/{len(steps)} 步。"
                  + (f" 卡住 {len(blocked)} 步。" if blocked else "")
                  + (f" 跳过 {len(skipped)} 步。" if skipped else ""))

        if blocked:
            # 汇总成一次提问，而不是每一步打断一次
            qs = "\n".join(
                f"{i}. 第{s['idx']}步「{s['desc'][:24]}」：{str(s['error'])[:80]}"
                for i, s in enumerate(blocked[:5], 1))
            report += f"\n需要你回答（/answer {tid} 你的答复）：\n{qs}"
            self.store.set_task(tid, status="needs_input", finished_at=time.time(),
                                report=report)
        else:
            self.store.set_task(tid, status="done", finished_at=time.time(),
                                report=report)
        self.store.add_event(tid, "note", report[:300])
        # 产物随最后一条消息回传（file 字段是 JSON 数组，插件负责上传到 QQ）
        outs = self._collect_outputs(tid)
        self.store.outbox_push(tid, self._final_message(task, steps, report),
                               json.dumps(outs, ensure_ascii=False) if outs else "")
        self._write_state(task)
        self._write_note(task, report)

    def _final_message(self, task: dict, steps: list[dict], report: str) -> str:
        """发给用户的最后一条：**先说结果，再说产物**。

        用户要的是「办成了什么」，不是「跑了几步」。所以：
        完成 → 最后一步的结论 + 产物文件名；
        卡住 → 编号问题（这条必须发，否则他不知道要答什么）。
        """
        tid = task["id"]
        if self.store.task(tid)["status"] == "needs_input":
            return report[:500]
        done = [s for s in steps if s["status"] == "done"]
        summary = ""
        for s in reversed(done):
            if str(s.get("result") or "").strip():
                summary = str(s["result"]).strip()[:200]
                break
        summary = summary or "做完了"
        files: list[str] = []
        try:
            d = self.work / str(tid)
            files = sorted(p.name for p in d.iterdir()
                           if p.is_file() and p.name != "PLAN.md")[:6]
        except Exception:  # noqa: BLE001
            pass
        msg = f"#{tid} {summary}"
        if files:
            # 摘要太敷衍（"done"/"ok"）时，产物才是真正的结果
            if len(summary.strip()) < 8:
                msg = f"#{tid} 完成"
            msg += "\n产物：" + "、".join(files)
        if len(done) < len(steps):
            msg += f"\n（{len(done)}/{len(steps)} 步完成）"
        return msg[:500]

    async def _replan(self, task: dict, steps: list[dict]) -> bool:
        """全部卡住时，允许重新规划一次（§六）。每任务只给一次机会，防止无限重规划。"""
        tid = task["id"]
        if self.store.replanned(tid):
            return False
        bad = [s for s in steps if s["status"] in ("blocked", "skipped")]
        if not bad:
            return False
        self.store.mark_replanned(tid)
        ctx = "\n".join(f"- 第{s['idx']}步「{s['desc'][:30]}」失败：{str(s['error'])[:80]}"
                        for s in bad[:5])
        ans = [e for e in self.store.events(tid, 20, ["answer"])]
        ans_text = str(ans[-1]["payload"])[:400] if ans else ""
        pseudo = dict(task)
        pseudo["goal"] = (f"{task['goal']}\n\n【已经失败的步骤，不要重复同样的做法】\n{ctx}"
                          + (f"\n\n【用户的答复，按这个来】\n{ans_text}" if ans_text else "")
                          + "\n请给出绕过这些失败的替代步骤。")
        try:
            new_steps = await self.planner.plan(pseudo)
        except Exception:  # noqa: BLE001
            return False
        if not new_steps:
            return False
        offset = max((s["idx"] for s in steps), default=-1) + 1
        for i, s in enumerate(new_steps):
            s["idx"] = offset + i
        self.store.add_steps(tid, new_steps)
        self._append_plan(tid, new_steps, reason=f"{len(bad)} 步失败")
        self.store.add_event(tid, "note", f"重规划：新增 {len(new_steps)} 步")
        return True

    def _append_plan(self, tid: int, steps: list[dict], reason: str = "") -> None:
        p = self.work / str(tid) / "PLAN.md"
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a", encoding="utf-8") as f:
                f.write(f"\n## 修订{'（' + reason + '）' if reason else ''}\n")
                for s in steps:
                    f.write(f"{s['idx']}. [ ] {s['desc']}\n")
        except Exception:  # noqa: BLE001
            pass

    # ---------- 项目状态（跨任务）----------
    def _write_note(self, task: dict, report: str) -> None:
        """把任务结果写进知识库笔记目录（单向）：陪伴机器人下一轮索引就能看到。

        为什么单向：机器人知道「他让我干过什么」有用；反过来让它影响 agent 的判断没必要。
        """
        d = Path(self.cfg.handoff_note_dir)
        if not d.exists():
            return
        try:
            day = time.strftime("%Y-%m-%d")
            f = d / f"{day}-任务{task['id']}.md"
            body = (f"# 任务 {task['id']}：{task['goal'][:40]}\n\n"
                    f"- 时间：{time.strftime('%Y-%m-%d %H:%M')}\n"
                    f"- 状态：{self.store.task(task['id'])['status']}\n"
                    f"- 结果：{report[:400]}\n")
            f.write_text(body, encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
    def _write_state(self, task: dict) -> None:
        """把「这个项目做到哪了」追加到 STATE.md，跨任务可见。"""
        project = (task.get("project") or "default").replace("/", "_")[:40]
        d = self.work / "projects" / project
        try:
            d.mkdir(parents=True, exist_ok=True)
            f = d / "STATE.md"
            t = self.store.task(task["id"]) or task
            line = (f"- #{task['id']} [{t.get('status')}] {task['goal'][:60]}"
                    f" — {time.strftime('%Y-%m-%d %H:%M')}\n")
            if not f.exists():
                f.write_text(f"# 项目 {project} 状态\n\n", encoding="utf-8")
            with f.open("a", encoding="utf-8") as fh:
                fh.write(line)
        except Exception:  # noqa: BLE001
            pass

    # ---------- 工具 ----------
    def _write_plan(self, tid: int, steps: list[dict]) -> None:
        d = self.work / str(tid)
        d.mkdir(parents=True, exist_ok=True)
        lines = [f"# 任务 {tid}", ""]
        for s in steps:
            lines.append(f"{s['idx']}. [ ] {s['desc']}  `{json.dumps(s['verify'], ensure_ascii=False)}`")
        (d / "PLAN.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------- 工具函数 ----------
def classify(obs: Observation, why: str, ok: bool) -> str:
    if ok:
        return "ok"
    text = (obs.stderr or "") + (obs.stdout or "") + why
    if "[OOM]" in text or "内存超限" in text:
        return "resource"
    if any(h in text for h in BLOCKED_HINTS):
        return "blocked"
    if obs.exit_code in (124, 137):
        return "transient"
    # 127 = 命令不存在。这不是「这条路走不通」，而是「用错了工具」，
    # 应该让它换等价命令重试（实测 `file` 没装时，整条任务被 step0 连坐跳过）。
    return "logic"


def _segments(cmd: str) -> list[list[str]]:
    """按 ; && || | 和换行切成独立命令，返回 token 列表。

    纯子串匹配的漏洞：rm -r -f、rm --recursive --force、rm $FLAGS 都能绕过去。
    解析成 argv 之后按「命令 + 参数组合」判断，绕过面小得多。
    解析失败（引号不配对等）时调用方退回子串匹配。
    """
    lex = shlex.shlex(cmd, posix=True, punctuation_chars=";&|\n")
    lex.whitespace_split = True
    toks = list(lex)
    out: list[list[str]] = []
    cur: list[str] = []
    for t in toks:
        if t and set(t) <= set(";&|\n"):
            if cur:
                out.append(cur)
                cur = []
        else:
            cur.append(t)
    if cur:
        out.append(cur)
    return out


_DANGER_CMDS = {"mkfs", "shutdown", "reboot", "poweroff", "halt", "init",
                "wipefs", "fdisk", "parted", "shred", "killall", "pkill"}
_RECURSIVE = {"-r", "-R", "--recursive"}
_PREFIXES = {"sudo", "doas", "env", "command", "nohup", "time", "exec"}


def _is_dangerous_argv(argv: list[str]) -> bool:
    if not argv:
        return False
    while argv and argv[0].lower() in _PREFIXES:
        argv = argv[1:]
    if not argv:
        return False
    head = argv[0].lower().rsplit("/", 1)[-1]
    args = argv[1:]

    if head in _DANGER_CMDS or head.startswith("mkfs"):
        return True

    if head == "dd":
        return any(a.startswith(("if=", "of=")) for a in args)

    if head == "rm":
        # 变量 / 命令替换会把真实参数藏起来（rm $FLAGS），看不到就按危险处理
        if any("$" in a or chr(96) in a for a in args):
            return True
        flags = [a for a in args if a.startswith("-")]
        if any(f in flags for f in _RECURSIVE):
            return True
        # 合写形态：-rf / -fr / -Rf / -rfi …
        if any(not f.startswith("--") and "r" in f.lower() and "f" in f.lower()
               for f in flags):
            return True
        return False

    if head == "chmod":
        return any(a == "777" for a in args) and any(f in _RECURSIVE for f in args)

    if head == "truncate":
        return any(a.startswith("-s0") for a in args)

    return False


def is_dangerous(cmd: str) -> bool:
    """要不要人工点头。宁可多问一句，也不要让模型一步清空工作区。"""
    low = (cmd or "").lower()
    # 这两类没法当普通命令解析，先兜住
    if ":(){" in low or "> /etc/" in low:
        return True
    try:
        segs = _segments(cmd)
    except Exception:  # noqa: BLE001
        return any(d in low for d in DANGER)
    if not segs:
        return any(d in low for d in DANGER)
    return any(_is_dangerous_argv(seg) for seg in segs)


def _sha(p: Path) -> str:
    try:
        return hashlib.sha256(p.read_bytes()).hexdigest()[:16]
    except Exception:  # noqa: BLE001
        return ""


def _changed_files(blob: dict, base: Path) -> list[str]:
    changed: list[str] = []
    for f in (blob.get("workspace") or {}).get("files") or []:
        p = base / f["path"]
        if not p.exists():
            changed.append(f["path"] + "（已删除）")
        elif _sha(p) != f.get("sha"):
            changed.append(f["path"])
    return changed
