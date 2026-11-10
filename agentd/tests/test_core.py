"""agentd 离线测试：不联网、不碰 docker，全部用假的 LLM 和沙箱。

跑法：python tests/test_core.py
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import time
from pathlib import Path

# 不用 tempfile：受限环境下系统临时目录不可写。默认放工作区临时目录，可用 AGENTD_TESTTMP 覆盖。
TMP = Path(os.environ.get("AGENTD_TESTTMP", Path.cwd() / ".agentd-test"))
shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True, exist_ok=True)
os.environ["AGENTD_ROOT"] = str(TMP)
os.environ["AGENTD_DATA"] = str(TMP / "data")
os.environ["AGENTD_WORK"] = str(TMP / "work")
os.environ["AGENTD_KEYS"] = str(TMP / "keys.json")
(TMP / "keys.json").write_text('{"deepseek":"test"}', encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentd import config  # noqa: E402
from agentd.context import Context  # noqa: E402
from agentd.llm import extract_json  # noqa: E402
from agentd.loop import Runner, attachment_hint, classify, _changed_files  # noqa: E402
from agentd.planner import Planner, noop_ratio, sanitize_steps  # noqa: E402
from agentd.sandbox import Sandbox, _head_tail  # noqa: E402
from agentd.store import Store  # noqa: E402
from agentd.tools import TOOLS_DOC, Tools  # noqa: E402
from agentd.verify import Observation, evaluate  # noqa: E402

PASS = FAIL = 0


def check(name: str, ok: bool, extra: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"PASS {name}")
    else:
        FAIL += 1
        print(f"FAIL {name} | {extra}")


class FakeLLM:
    """按 purpose 排队返回预设文本。"""

    def __init__(self, plans=None, actions=None, compacts=None) -> None:
        self.cfg = config.Config.load()
        self.plans = list(plans or [])
        self.actions = list(actions or [])
        self.compacts = list(compacts or [])
        self.calls: list[str] = []

    async def chat(self, messages, purpose, *, task_id=None, step_id=None, **kw):
        self.calls.append(purpose)
        if purpose == "plan":
            return self.plans.pop(0) if self.plans else json.dumps(
                {"steps": [{"idx": 0, "desc": "写文件", "verify": {"file_exists": "a.txt"}}]})
        if purpose == "compact":
            return self.compacts.pop(0) if self.compacts else "摘要"
        if purpose == "reanchor":
            return "重锚定后的摘要"
        return self.actions.pop(0) if self.actions else json.dumps(
            {"tool": "finish", "args": {"summary": "好了"}})


class FakeSandbox(Sandbox):
    """不碰 docker：按命令前缀返回预设结果。"""

    def __init__(self, cfg, work, script=None) -> None:
        super().__init__(cfg, work)
        self.script = script or {}
        self.commands: list[str] = []

    async def ensure(self) -> bool:
        return True

    async def oom(self) -> bool:
        return False

    async def exec(self, cmd: str, task_id: int, timeout=None):
        self.commands.append(cmd)
        for key, obs in self.script.items():
            if key in cmd:
                return obs, ""
        return Observation(exit_code=0, stdout="ok"), ""


def build(store, cfg, llm, sb, work):
    tools = Tools(cfg, sb, work)
    ctx = Context(cfg, store, llm, TOOLS_DOC, work=work)
    planner = Planner(cfg, store, llm)
    return Runner(cfg, store, llm, sb, tools, ctx, planner, work)


async def main() -> None:
    cfg = config.Config.load()
    cfg.max_tokens = 50000
    store = Store(TMP / "data" / "t.db")
    work = TMP / "work"

    # ---------- verify DSL ----------
    check("verify exit_code", evaluate({"exit_code": 0}, Observation(exit_code=0))[0])
    check("verify exit_code 失败", not evaluate({"exit_code": 0}, Observation(exit_code=1))[0])
    check("verify file_exists+min_size",
          evaluate({"file_exists": "a.txt", "min_size": 5},
                   Observation(files={"a.txt": 10}))[0])
    check("verify 文件太小失败",
          not evaluate({"file_exists": "a.txt", "min_size": 50},
                       Observation(files={"a.txt": 10}))[0])
    check("verify contains", evaluate({"contains": "异常"}, Observation(stdout="有 3 处异常"))[0])
    check("verify regex", evaluate({"regex": r"\d+ 条"}, Observation(stdout="12 条"))[0])
    ok, why, unv = evaluate({"noop": True}, Observation())
    check("verify noop 通过但标记未验证", ok and unv)
    check("verify custom 调用 runner",
          evaluate({"custom": "check.py"}, Observation(),
                   runner=lambda c: (0, ""))[0])
    check("未知条件不放行", not evaluate({"foo": 1}, Observation())[0])

    # ---------- 路径逃逸 ----------
    tools = Tools(cfg, FakeSandbox(cfg, work), work)
    (work / "7").mkdir(parents=True, exist_ok=True)
    try:
        tools._safe(7, "../../etc/passwd")
        check("路径逃逸被拦截", False, "没有抛异常")
    except PermissionError:
        check("路径逃逸被拦截", True)
    check("正常路径放行", Path(tools._safe(7, "a.txt")).name == "a.txt")

    # ---------- 验收：file_exists 要看真实文件 ----------
    (work / "8").mkdir(parents=True, exist_ok=True)
    (work / "8" / "real.txt").write_text("x" * 10, encoding="utf-8")
    r8 = build(store, cfg, FakeLLM(), FakeSandbox(cfg, work), work)
    check("resolver 认相对路径", r8._resolve_file(8, "real.txt") == 10)
    check("resolver 认 /work 前缀", r8._resolve_file(8, "/work/real.txt") == 10)
    check("resolver 认 /work/<id> 前缀", r8._resolve_file(8, "/work/8/real.txt") == 10)
    check("resolver 拒绝逃逸", r8._resolve_file(8, "../t7/x") is None)
    check("file_exists 用 resolver 通过",
          evaluate({"file_exists": "real.txt"},
                   Observation(), resolver=lambda p: r8._resolve_file(8, p))[0])

    # ---------- 溢出落盘 ----------
    big = "\n".join(f"line{i}" for i in range(500))
    cut = _head_tail(big, 4096, "/tmp/x.log")
    check("溢出保留头尾并给路径", "line0" in cut and "line499" in cut and "/tmp/x.log" in cut)

    # ---------- 失败分类 ----------
    check("OOM → resource", classify(Observation(exit_code=137, stderr="[OOM]"), "", False) == "resource")
    check("权限 → blocked", classify(Observation(exit_code=1, stderr="Permission denied"), "", False) == "blocked")
    check("137 → transient", classify(Observation(exit_code=137), "", False) == "transient")
    check("其它 → logic", classify(Observation(exit_code=1), "不匹配", False) == "logic")

    # ---------- 计划器的 noop 拦截 ----------
    bad_plan = json.dumps({"steps": [
        {"idx": 0, "desc": "a", "verify": {"noop": True}},
        {"idx": 1, "desc": "b", "verify": {"noop": True}},
        {"idx": 2, "desc": "c", "verify": {"exit_code": 0}},
    ]})
    good_plan = json.dumps({"steps": [
        {"idx": 0, "desc": "a", "verify": {"exit_code": 0}},
        {"idx": 1, "desc": "b", "verify": {"contains": "x"}},
    ]})
    pl_llm = FakeLLM(plans=[bad_plan, good_plan])
    planner = Planner(cfg, store, pl_llm)
    tid = store.create_task("测试计划")
    plan = await planner.plan(store.task(tid))
    check("noop 超比例会重写计划", len(plan) == 2 and noop_ratio(plan) == 0, str(plan))

    check("重写记了事件", any("noop" in (e["payload"] or "")
                              for e in store.events(tid, 20)))

    # 计划里的 custom 只能引用已经创建的脚本，否则必然等一个不存在的文件
    _bad = sanitize_steps([
        {"idx": 0, "desc": "写脚本 /work/1/check.py",
         "verify": {"custom": "python /work/1/check.py"}},
        {"idx": 1, "desc": "运行别的脚本",
         "verify": {"custom": "python /work/1/never.py"}},
    ])
    check("已创建的 custom 保留", _bad[0]["verify"].get("custom") is not None, _bad[0])
    check("未创建的 custom 降级", _bad[1]["verify"] == {"exit_code": 0}, _bad[1])
    check("附件提示认这份", attachment_hint("把这份简历重排"), "未命中")
    check("附件提示不拦生成", not attachment_hint("生成一个 CSV"))

    # ---------- 端到端：两步任务 ----------
    store2 = Store(TMP / "data" / "t2.db")
    plan2 = json.dumps({"steps": [
        {"idx": 0, "desc": "写文件", "verify": {"file_exists": "a.txt", "min_size": 1}},
        {"idx": 1, "desc": "确认", "verify": {"exit_code": 0}},
    ]})
    actions = [
        json.dumps({"tool": "write", "args": {"path": "a.txt", "content": "hello"}}),
        json.dumps({"tool": "finish", "args": {"summary": "写好了"}}),
        json.dumps({"tool": "shell", "args": {"cmd": "echo ok"}}),
        json.dumps({"tool": "finish", "args": {"summary": "确认完了"}}),
    ]
    llm2 = FakeLLM(plans=[plan2], actions=actions, compacts=["压缩后的摘要"])
    sb2 = FakeSandbox(cfg, work, {"echo ok": Observation(exit_code=0, stdout="ok")})
    r2 = build(store2, cfg, llm2, sb2, work)
    t2 = store2.create_task("两步任务")
    await r2.run_task(store2.task(t2))
    t = store2.task(t2)
    check("任务完成", t["status"] == "done", str(t))
    check("产物真的写了", (work / str(t2) / "a.txt").exists())
    check("两步都 done",
          [s["status"] for s in store2.steps(t2)] == ["done", "done"],
          str([s["status"] for s in store2.steps(t2)]))
    check("有检查点", store2.last_checkpoint(t2) is not None)
    _msgs = store2.outbox_since(0)
    # 文件进出：create_task 带 files → extra 里存住 → 搬进工作区 → 产物收集
    _tid = store.create_task("改文档", files=["/tmp/in/a.docx"])
    _t = store.task(_tid)
    import json as _json
    check("files 存进了 extra",
          (_json.loads(_t.get("extra") or "{}").get("files") or []) == ["/tmp/in/a.docx"],
          str(_t.get("extra"))[:60])

    check("快任务只发一条结果消息", len(_msgs) == 1, str(len(_msgs)))
    check("最后一条是结果不是过程", "产物" in _msgs[0]["text"] or "完成" in _msgs[0]["text"],
          _msgs[0]["text"][:60])
    check("记账有 plan/decide", {"plan", "decide"} <= set(llm2.calls), str(set(llm2.calls)))

    # ---------- 阻塞传播 ----------
    store3 = Store(TMP / "data" / "t3.db")
    plan3 = json.dumps({"steps": [
        {"idx": 0, "desc": "需要权限", "verify": {"exit_code": 0}},
        {"idx": 1, "desc": "依赖上一步", "depends_on": [0], "verify": {"exit_code": 0}},
    ]})
    llm3 = FakeLLM(plans=[plan3], actions=[
        json.dumps({"tool": "shell", "args": {"cmd": "cat /root/secret"}})] * 3)
    sb3 = FakeSandbox(cfg, work, {
        "cat /root/secret": Observation(exit_code=1, stderr="Permission denied")})
    r3 = build(store3, cfg, llm3, sb3, work)
    t3 = store3.create_task("阻塞任务")
    await r3.run_task(store3.task(t3))
    st3 = [s["status"] for s in store3.steps(t3)]
    check("阻塞步骤标 blocked", st3[0] == "blocked", str(st3))
    check("依赖它的步骤被跳过", st3[1] == "skipped", str(st3))
    check("卡住会转成待回答并给出报告",
          store3.task(t3)["status"] == "needs_input"
          and "/answer" in (store3.task(t3).get("report") or ""),
          str(store3.task(t3)))

    # ---------- 预算 ----------
    store4 = Store(TMP / "data" / "t4.db")
    cfg4 = config.Config.load()
    cfg4.max_tokens = 0
    llm4 = FakeLLM(plans=[plan2])
    r4 = build(store4, cfg4, llm4, FakeSandbox(cfg4, work), work)
    t4 = store4.create_task("预算任务")
    await r4.run_task(store4.task(t4))
    check("预算触顶直接失败", store4.task(t4)["status"] == "failed",
          str(store4.task(t4)["status"]))

    # ---------- 重锚定 ----------
    store5 = Store(TMP / "data" / "t5.db")
    t5 = store5.create_task("重锚任务")
    store5.set_task(t5, status="running")
    base = work / str(t5)
    base.mkdir(parents=True, exist_ok=True)
    f = base / "data.csv"
    f.write_text("a\n", encoding="utf-8")
    store5.save_checkpoint(t5, 0, {"workspace": {"files": [
        {"path": "data.csv", "size": 2, "sha": "deadbeef"}]}})
    llm5 = FakeLLM()
    r5 = build(store5, cfg, llm5, FakeSandbox(cfg, work), work)
    await r5._reanchor(store5.task(t5), [])
    check("文件变了会重锚定", "重锚定" in r5.ctx.digest(store5.task(t5)),
          r5.ctx.digest(store5.task(t5)))

    # ---------- 队列 FIFO ----------
    store6 = Store(TMP / "data" / "t6.db")
    a = store6.create_task("先来")
    b = store6.create_task("后到")
    check("队列先进先出", store6.next_pending_task()["id"] == a, str(a))

    # ---------- JSON 抽取 ----------
    check("从 ```json 块里抽", extract_json('```json\n{"tool":"x"}\n```') == {"tool": "x"})
    check("从裸文本里抽", extract_json('前缀 {"a":1} 后缀') == {"a": 1})
    check("没有 JSON 返回空", extract_json("没有") == {})
    check("截断的 JSON 能修回来",
          extract_json('{"tool":"shell","args":{"cmd":"echo hi') ==
          {"tool": "shell", "args": {"cmd": "echo hi"}},
          str(extract_json('{"tool":"shell","args":{"cmd":"echo hi')))
    check("截断在数组里也能修",
          extract_json('{"steps":[{"idx":0,"desc":"a","verify":{"exit_code":0}}')
          .get("steps", [{}])[0].get("desc") == "a")

    # ---------- 暂停后不再推进 ----------
    store7 = Store(TMP / "data" / "t7.db")
    t7 = store7.create_task("暂停任务")
    store7.set_task(t7, status="paused")
    r7 = build(store7, cfg, FakeLLM(), FakeSandbox(cfg, work), work)
    await r7.run_task(store7.task(t7))
    check("暂停的任务不执行", store7.task(t7)["status"] == "paused",
          store7.task(t7)["status"])

    # ---------- 崩溃恢复：running 步骤要重置 ----------
    store8 = Store(TMP / "data" / "t8.db")
    t8 = store8.create_task("恢复任务")
    store8.set_task(t8, status="running")
    store8.add_steps(t8, [
        {"idx": 0, "desc": "被中断的一步", "verify": {"exit_code": 0}},
        {"idx": 1, "desc": "收尾", "depends_on": [0], "verify": {"exit_code": 0}},
    ])
    steps8 = store8.steps(t8)
    store8.set_step(steps8[0]["id"], status="running")     # 模拟被杀
    llm8 = FakeLLM(actions=[
        json.dumps({"tool": "shell", "args": {"cmd": "echo ok"}}),
        json.dumps({"tool": "finish", "args": {"summary": "好了"}}),
        json.dumps({"tool": "finish", "args": {"summary": "完了"}}),
    ])
    r8b = build(store8, cfg, llm8, FakeSandbox(cfg, work, {
        "echo ok": Observation(exit_code=0, stdout="ok")}), work)
    await r8b.run_task(store8.task(t8))
    check("中断的步骤被重置并跑完",
          [s["status"] for s in store8.steps(t8)] == ["done", "done"],
          str([s["status"] for s in store8.steps(t8)]))
    check("恢复有事件记录", any("恢复" in (e["payload"] or "")
                                for e in store8.events(t8, 20)))

    # ---------- 审批：危险命令要人工点头 ----------
    from agentd.loop import is_dangerous

    check("rm -rf 判为危险", is_dangerous("rm -rf /work/1"))
    check("普通命令不拦", not is_dangerous("echo hi"))
    check("rm -r -f 也拦（分开写）", is_dangerous("rm -r -f /work/1"))
    check("rm --recursive --force 也拦", is_dangerous("rm --recursive --force x"))
    check("rm 变量展开保守拦截", is_dangerous("rm $FLAGS x"))
    check("普通 rm 不拦", not is_dangerous("rm a.txt"))
    check("引号里的 rm -rf 不误判", not is_dangerous('echo "rm -rf /"'))
    check("分号串联照样拦", is_dangerous("ls; rm -rf /"))
    check("sudo 前缀不影响判断", is_dangerous("sudo /bin/rm -rf /tmp/x"))
    check("dd if= 仍拦", is_dangerous("dd if=/dev/zero of=/dev/sda"))
    store9 = Store(TMP / "data" / "t9.db")
    plan9 = json.dumps({"steps": [
        {"idx": 0, "desc": "危险操作", "verify": {"exit_code": 0}}]})
    llm9 = FakeLLM(plans=[plan9], actions=[
        json.dumps({"tool": "shell", "args": {"cmd": "rm -rf important"}})])
    sb9 = FakeSandbox(cfg, work)
    r9 = build(store9, cfg, llm9, sb9, work)
    t9 = store9.create_task("危险任务")
    await r9.run_task(store9.task(t9))
    pend = store9.approvals("pending")
    check("危险命令转为待审批", len(pend) == 1, str(pend))
    check("没执行沙箱命令", not sb9.commands, str(sb9.commands))
    check("任务没被判完成", store9.task(t9)["status"] == "needs_input",
          store9.task(t9)["status"])

    # 批准后可以执行一次
    a = pend[0]
    store9.set_approval(a["id"], "approved")
    check("批准后放行一次", store9.approved_cmd(a["task_id"], a["step_id"], a["cmd"]))
    check("用过就作废", not store9.approved_cmd(a["task_id"], a["step_id"], a["cmd"]))

    # ---------- 用量与清理 ----------
    store9.record_call(t9, "decide", 100, 20, 5)
    _b = store9.call_breakdown()
    check("用量按用途分组", _b.get("decide", {}).get("tokens") == 120, str(_b))
    _ = store9.cleanup_workspace(work, keep_tasks=100, log_days=0)
    check("清理不报错", True)

    # ---------- custom 验收必须真的在沙箱里跑 ----------
    store10 = Store(TMP / "data" / "t10.db")
    plan10 = json.dumps({"steps": [
        {"idx": 0, "desc": "写文件并自检", "verify": {"custom": "test -f ok.txt"}}]})
    llm10 = FakeLLM(plans=[plan10], actions=[
        json.dumps({"tool": "write", "args": {"path": "ok.txt", "content": "x"}}),
        json.dumps({"tool": "finish", "args": {"summary": "ok"}}),
    ])
    sb10 = FakeSandbox(cfg, work, {"test -f ok.txt": Observation(exit_code=0)})
    r10 = build(store10, cfg, llm10, sb10, work)
    t10 = store10.create_task("自定义验收")
    await r10.run_task(store10.task(t10))
    check("custom 验收真的跑了沙箱命令", "test -f ok.txt" in sb10.commands,
          str(sb10.commands))
    check("custom 通过则步骤完成", store10.steps(t10)[0]["status"] == "done",
          str([s["status"] for s in store10.steps(t10)]))

    store11 = Store(TMP / "data" / "t11.db")
    sb11 = FakeSandbox(cfg, work, {"test -f bad.txt": Observation(exit_code=1)})
    llm11 = FakeLLM(
        plans=[json.dumps({"steps": [
            {"idx": 0, "desc": "自检失败", "verify": {"custom": "test -f bad.txt"}}]})],
        actions=[json.dumps({"tool": "write",
                             "args": {"path": "bad.txt", "content": "x"}})] * 3)
    r11 = build(store11, cfg, llm11, sb11, work)
    t11 = store11.create_task("自定义验收失败")
    await r11.run_task(store11.task(t11))
    check("custom 失败则不放行", store11.steps(t11)[0]["status"] == "blocked",
          str([s["status"] for s in store11.steps(t11)]))

    # custom 验收本身也可能是一条危险命令：同样要审批，且批准前不许执行
    store_c = Store(TMP / "data" / "t_custom_danger.db")
    plan_c = json.dumps({"steps": [
        {"idx": 0, "desc": "危险验收", "verify": {"custom": "rm -rf /work/1"}}]})
    llm_c = FakeLLM(plans=[plan_c], actions=[
        json.dumps({"tool": "write", "args": {"path": "ok.txt", "content": "x"}})])
    sb_c = FakeSandbox(cfg, work, {"rm -rf /work/1": Observation(exit_code=0)})
    r_c = build(store_c, cfg, llm_c, sb_c, work)
    t_c = store_c.create_task("危险自定义验收")
    await r_c.run_task(store_c.task(t_c))
    pend_c = store_c.approvals("pending")
    check("custom 里的危险命令也要审批", len(pend_c) == 1, str(pend_c))
    check("custom 未批准不执行", not sb_c.commands, str(sb_c.commands))
    check("custom 审批前任务不完成",
          store_c.task(t_c)["status"] == "needs_input", store_c.task(t_c)["status"])

    # ---------- 批量提问 + /answer ----------
    store12 = Store(TMP / "data" / "t12.db")
    t12 = store12.create_task("会卡住的任务", project="proj1")
    store12.set_task(t12, status="running")
    store12.add_steps(t12, [{"idx": 0, "desc": "第一步", "verify": {"exit_code": 0}}])
    st12 = store12.steps(t12)
    store12.set_step(st12[0]["id"], status="blocked", error="没权限")
    store12.set_task(t12, status="needs_input")
    check("回答前是 needs_input", store12.task(t12)["status"] == "needs_input")
    n = store12.answer(t12, "用 /tmp 目录代替")
    check("回答后卡住的步骤作废并允许重规划",
          n == 1 and store12.steps(t12)[0]["status"] == "skipped"
          and store12.task(t12)["status"] == "pending"
          and not store12.replanned(t12), str(n))
    check("答复进了事件",
          any(e["kind"] == "answer" for e in store12.events(t12, 10)))

    # ---------- 重规划 ----------
    store13 = Store(TMP / "data" / "t13.db")
    plan_bad = json.dumps({"steps": [
        {"idx": 0, "desc": "必失败", "verify": {"exit_code": 0}}]})
    plan_fix = json.dumps({"steps": [
        {"idx": 0, "desc": "换条路", "verify": {"exit_code": 0}}]})
    llm13 = FakeLLM(plans=[plan_bad, plan_fix], actions=[
        json.dumps({"tool": "shell", "args": {"cmd": "false"}})] * 3 + [
        json.dumps({"tool": "finish", "args": {"summary": "搞定"}}),
    ])
    sb13 = FakeSandbox(cfg, work, {"false": Observation(exit_code=1)})
    r13 = build(store13, cfg, llm13, sb13, work)
    t13 = store13.create_task("需要重规划")
    await r13.run_task(store13.task(t13))
    idxs = [s["idx"] for s in store13.steps(t13)]
    check("失败后追加了重规划步骤", len(idxs) >= 2 and max(idxs) >= 1, str(idxs))
    check("重规划只做一次", store13.replanned(t13))

    # ---------- 项目状态文件 ----------
    store14 = Store(TMP / "data" / "t14.db")
    llm14 = FakeLLM(plans=[json.dumps({"steps": [
        {"idx": 0, "desc": "干点小事", "verify": {"exit_code": 0}}]})],
        actions=[json.dumps({"tool": "finish", "args": {"summary": "ok"}})])
    r14 = build(store14, cfg, llm14, FakeSandbox(cfg, work), work)
    t14 = store14.create_task("写项目状态", project="proj1")
    await r14.run_task(store14.task(t14))
    sp = work / "projects" / "proj1" / "STATE.md"
    check("收尾会写项目状态", sp.exists() and f"#{t14}" in sp.read_text(encoding="utf-8"),
          str(sp))

    # ---------- sandbox：唯一没被测的高危模块（用假 argv，不碰 docker） ----------
    class FakeDocker(Sandbox):
        """按 argv 里的关键字返回预设结果，不碰 docker。"""

        def __init__(self, cfg, work, status="running", oom="false",
                     exec_result=(0, "", "")):
            super().__init__(cfg, work)
            self.status = status
            self.oom_flag = oom
            self.exec_result = exec_result
            self.seen: list[list[str]] = []

        async def _run_argv(self, argv, timeout=60):
            self.seen.append(list(argv))
            joined = " ".join(argv)
            if "State.Status" in joined:
                return (0, self.status + "\n", "")
            if "OOMKilled" in joined:
                return (0, self.oom_flag + "\n", "")
            if "exec" in joined:
                return self.exec_result
            return (0, "", "")

    sbx = FakeDocker(cfg, work)
    check("sandbox.state 解析", await sbx.state() == "running")
    check("sandbox.ensure 已在跑", await sbx.ensure() is True)
    await sbx.exec("echo hi", 999)
    check("sandbox.exec 带超时包裹",
          any("timeout -s KILL 30" in a for a in sbx.seen[-1]), str(sbx.seen[-1][-1:]))

    sbx_oom = FakeDocker(cfg, work, oom="true", exec_result=(137, "", "killed"))
    obs_oom, _ = await sbx_oom.exec("python big.py", 999)
    check("137+OOM 标出内存超限", "[OOM]" in obs_oom.stderr, obs_oom.stderr)

    sbx_to = FakeDocker(cfg, work, oom="false", exec_result=(137, "", ""))
    obs_to, _ = await sbx_to.exec("sleep 99", 999)
    check("137 非 OOM 不误标", "[OOM]" not in obs_to.stderr, obs_to.stderr)

    big_out = "\n".join(f"l{i}" for i in range(1000))
    sbx_big = FakeDocker(cfg, work, exec_result=(0, big_out, ""))
    obs_big, logpath = await sbx_big.exec("cat big", 7)
    check("溢出落盘并保留头尾",
          logpath and Path(logpath).exists() and "l0" in obs_big.stdout
          and "l999" in obs_big.stdout, str(logpath))

    # ---------- 等待超时 ----------
    store15 = Store(TMP / "data" / "t15.db")
    t15 = store15.create_task("等回答")
    store15.set_task(t15, status="needs_input", finished_at=time.time() - 90000)
    check("等太久的任务会被关闭", store15.expire_waiting(24) == 1
          and store15.task(t15)["status"] == "failed")

    print("\n=== %d/%d 通过 ===" % (PASS, PASS + FAIL))
    if FAIL:
        sys.exit(1)


asyncio.run(main())
