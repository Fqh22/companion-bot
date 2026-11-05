"""agent 插件：把 agentd（本机 8100）接到 QQ。

分工：agentd 负责干活，这里只做三件事——
1. 把 `/task` 转成任务；
2. 每 2 秒拉一次发件箱，把进度和结果发到 QQ；
3. 提供 /tasks /progress /cancel /pause /resume。

设计取舍：
- 轮询而不是让 agentd 直连 OneBot：不重复占一条连接，也不把 token 散到两个进程。
- 全部命令 `permission=SUPERUSER`（on_command 默认连群聊一起匹配）。
- agentd 挂了不影响聊天：所有调用都 try/except，失败只记 debug 日志。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import time

from nonebot import get_bot, get_driver, on_command
from nonebot.adapters.onebot.v11 import Bot, MessageEvent
from nonebot.log import logger
from nonebot.permission import SUPERUSER
from app_core import paths as _paths
from app_core import files as files_mod

API = "http://127.0.0.1:8100"
POLL_SECONDS = 2.0
# 游标必须落盘：放内存里的话，每次重启都从 0 开始，会把历史消息重发一遍（刷屏惨案）
CURSOR_FILE = _paths.DATA / "agent-cursor.json"
# 文件状态挪到了 app_core/files.py：**收到文件的那一方（chat）负责登记**，
# 这里只读。以前两边各下一遍，同一份 PDF 白走两次网络（2026-11-05 合并）。

_task_cmd = on_command("task", priority=4, block=True, permission=SUPERUSER)
_tasks_cmd = on_command("tasks", priority=4, block=True, permission=SUPERUSER)
_progress_cmd = on_command("progress", priority=4, block=True, permission=SUPERUSER)
_cancel_cmd = on_command("cancel", priority=4, block=True, permission=SUPERUSER)
_pause_cmd = on_command("pause", priority=4, block=True, permission=SUPERUSER)
_resume_cmd = on_command("resume", priority=4, block=True, permission=SUPERUSER)
_approve_cmd = on_command("approve", priority=4, block=True, permission=SUPERUSER)
# 注意别和 commands 的 /usage 撞名：这里 priority=4、block=True，
# 撞上会把那条整个吞掉（曾经就是这样——/usage 永远只出 agentd 的账，
# 聊天费用那条命令从来没执行过）。agentd 的单独叫 /agent_usage。
_usage_cmd = on_command("agent_usage", priority=4, block=True, permission=SUPERUSER)
_answer_cmd = on_command("answer", priority=4, block=True, permission=SUPERUSER)

_cursor = {"id": 0}
# 同一条消息连续发失败几次（防止一条永远发不出去的坏消息把发件箱卡死）
_fail = {"id": 0, "n": 0}


# ---------- 文件进出 ----------
def _pending(session: str) -> list[dict]:
    """最近收到的那个文件；超过 30 分钟就当没发过。登记表由 chat 写，这里只读。"""
    info = files_mod.pending()
    if not info:
        return []
    return [{"path": info["path"], "name": Path(info["path"]).name}]


def _clear_pending(session: str) -> None:
    files_mod.clear_pending()


async def _append_history(session: str, text: str) -> None:
    """把 agentd 发出去的话补进聊天历史。

    否则会出现「刚改完文件，接着问它改得怎么样，它一脸茫然」——
    同一个人格做了两件事，但记忆只有一半。
    """
    if not text.strip():
        return
    try:
        import aiosqlite

        async with aiosqlite.connect(_paths.DATA / "chat.db") as db:
            await db.execute(
                "INSERT INTO messages(session,role,content,ts) VALUES(?,?,?,?)",
                (session, "assistant", text[:2000], time.time()))
            await db.commit()
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[agent] 写历史失败：{e}")


async def _send_files(bot: Bot, uid: int, paths: list[str]) -> None:
    """把产物传回 QQ。单个失败不影响其他。"""
    for p in paths:
        try:
            f = Path(str(p))
            if not f.is_file():
                continue
            await bot.call_api("upload_private_file", user_id=uid,
                               file=str(f), name=f.name)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[agent] 传文件失败 {p}: {e}")


def _owner() -> int:
    """发给谁：配置里的第一个超管。"""
    try:
        users = list(get_driver().config.superusers or [])
        return int(users[0]) if users else 0
    except Exception:  # noqa: BLE001
        return 0


def _arg(text: str, cmd: str) -> str:
    return (text or "").replace(cmd, "", 1).strip()


async def _post(path: str, body: dict) -> dict:
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.post(f"{API}{path}", json=body)
        return r.json()


async def _get(path: str, params: dict | None = None) -> dict:
    async with httpx.AsyncClient(timeout=3) as c:
        r = await c.get(f"{API}{path}", params=params or {})
        return r.json()


async def _say(text: str) -> bool:
    """发一条私聊。返回是否真的发出去了——调用方要靠它决定推不推进游标。

    以前这里吞掉异常返回 None，外面照样 acks + 推进游标：
    断连那 30 秒里 agentd 干完活的报告就被永久丢掉了，而聊天历史里还留着
    「它说过这句话」的假记录。
    """
    uid = _owner()
    if not uid:
        return False
    try:
        await get_bot().call_api("send_private_msg", user_id=uid, message=text)
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[agent] 发送失败：{type(e).__name__}: {e}")
        return False


@_task_cmd.handle()
async def _(event: MessageEvent):
    goal = _arg(event.get_plaintext(), "/task")
    if not goal:
        await _task_cmd.finish("用法：/task 要办的事")
    session = f"private:{event.user_id}"
    files = _pending(session)
    body = {"goal": goal, "session": session}
    if files:
        body["files"] = [f["path"] for f in files]
    try:
        res = await _post("/task", body)
    except Exception as e:  # noqa: BLE001
        await _task_cmd.finish(f"agentd 没响应（{type(e).__name__}），先起服务")
    if not res.get("ok"):
        await _task_cmd.finish(f"没建成：{res.get('error')}")
    if files:
        _clear_pending(session)
        await _task_cmd.finish(f"收到 #{res['id']}，带 {len(files)} 个文件，我先看看")
    await _task_cmd.finish(f"收到 #{res['id']}，我先看看")


@_tasks_cmd.handle()
async def _():
    try:
        res = await _get("/tasks", {"limit": 8})
    except Exception as e:  # noqa: BLE001
        await _tasks_cmd.finish(f"agentd 没响应（{type(e).__name__}）")
    rows = res.get("tasks") or []
    if not rows:
        await _tasks_cmd.finish("还没有任务")
    await _tasks_cmd.finish("\n".join(
        f"#{t['id']} [{t['status']}] {t['goal'][:24]}" for t in rows))


@_progress_cmd.handle()
async def _(event: MessageEvent):
    tid = _arg(event.get_plaintext(), "/progress")
    if not tid.isdigit():
        await _progress_cmd.finish("用法：/progress 任务号")
    try:
        res = await _get("/tasks", {"limit": 20})
    except Exception as e:  # noqa: BLE001
        await _progress_cmd.finish(f"agentd 没响应（{type(e).__name__}）")
    for t in res.get("tasks") or []:
        if str(t["id"]) == tid:
            await _progress_cmd.finish(
                f"#{tid} [{t['status']}]\n{t['goal'][:40]}\n{t.get('report') or '进行中'}")
    await _progress_cmd.finish(f"没有 #{tid}")


async def _control(action: str, event: MessageEvent, cmd: str) -> None:
    tid = _arg(event.get_plaintext(), f"/{cmd}")
    if not tid.isdigit():
        await _say(f"用法：/{cmd} 任务号")
        return
    try:
        res = await _post(f"/task/{tid}/{action}", {})
    except Exception as e:  # noqa: BLE001
        await _say(f"agentd 没响应（{type(e).__name__}）")
        return
    await _say("好了" if res.get("ok") else f"没成功：{res.get('error')}")


@_cancel_cmd.handle()
async def _(event: MessageEvent):
    await _control("cancel", event, "cancel")


@_pause_cmd.handle()
async def _(event: MessageEvent):
    await _control("pause", event, "pause")


@_resume_cmd.handle()
async def _(event: MessageEvent):
    await _control("resume", event, "resume")


@_approve_cmd.handle()
async def _(event: MessageEvent):
    """批准一条危险命令（agentd 会把它执行的步骤重新排队）。"""
    aid = _arg(event.get_plaintext(), "/approve")
    if not aid.isdigit():
        await _approve_cmd.finish("用法：/approve 审批号")
    try:
        res = await _post(f"/approve/{aid}", {})
    except Exception as e:  # noqa: BLE001
        await _approve_cmd.finish(f"agentd 没响应（{type(e).__name__}）")
    await _approve_cmd.finish(f"已批准 #{aid}" if res.get("ok")
                              else f"没成功：{res.get('error')}")


@_answer_cmd.handle()
async def _(event: MessageEvent):
    """回答一个卡住的任务：/answer 任务号 你的答复"""
    raw = _arg(event.get_plaintext(), "/answer")
    parts = raw.split(maxsplit=1)
    if len(parts) < 2 or not parts[0].isdigit():
        await _answer_cmd.finish("用法：/answer 任务号 你的答复")
    tid, text = parts[0], parts[1].strip()
    try:
        res = await _post(f"/task/{tid}/answer", {"text": text})
    except Exception as e:  # noqa: BLE001
        await _answer_cmd.finish(f"agentd 没响应（{type(e).__name__}）")
    if not res.get("ok"):
        await _answer_cmd.finish(f"没成功：{res.get('error')}")
    await _answer_cmd.finish(f"# {tid} 收到答复，重新开了 {res.get('reopened', 0)} 步")


@_usage_cmd.handle()
async def _():
    try:
        res = await _get("/usage")
    except Exception as e:  # noqa: BLE001
        await _usage_cmd.finish(f"agentd 没响应（{type(e).__name__}）")
    parts = res.get("by_purpose") or {}
    lines = [f"今天 {res.get('tokens_today', 0)} tokens"]
    lines += [f"{k}: {v['calls']} 次 / {v['tokens']} tokens" for k, v in parts.items()]
    await _usage_cmd.finish("\n".join(lines))


async def _poll_once() -> None:
    """拉发件箱 → 发到 QQ → 回执游标。失败不推进游标，下一轮重试。"""
    try:
        res = await _get("/outbox", {"since": _cursor["id"]})
    except Exception:  # noqa: BLE001
        return
    msgs = res.get("messages") or []
    if not msgs:
        return
    if not _owner():
        logger.warning("[agent] admin.user_id 未配置：发件箱里的消息发不出去，先不推进游标")
        return
    bot = None
    try:
        bot = get_bot()
    except Exception:  # noqa: BLE001
        pass
    for m in msgs:
        text = str(m.get("text") or "")
        if not await _say(text):
            # 发不出去就**停在这条**：不写历史、不推进游标，下一轮从同一条重试。
            # 连续失败 3 次才放弃（否则一条坏消息会把队列卡死），并打 ERROR 留下内容。
            mid = int(m.get("id") or 0)
            if _fail["id"] == mid:
                _fail["n"] += 1
            else:
                _fail.update({"id": mid, "n": 1})
            if _fail["n"] < 3:
                logger.warning(f"[agent] 第 {_fail['n']} 次发送失败，游标停在第 {mid} 条")
                return
            logger.error(f"[agent] 连续 {_fail['n']} 次发不出去，跳过第 {mid} 条：{text[:120]}")
        else:
            _fail.update({"id": 0, "n": 0})
        if text.strip():
            await _append_history(f"private:{_owner()}", text)
        raw = str(m.get("file") or "")
        if raw and bot is not None:
            try:
                files = json.loads(raw)
            except Exception:  # noqa: BLE001
                files = [raw]
            if isinstance(files, list) and files:
                await _send_files(bot, _owner(), [str(x) for x in files])
    _cursor["id"] = int(msgs[-1]["id"])
    _save_cursor(_cursor["id"])
    try:
        await _post("/outbox/ack", {"up_to": _cursor["id"]})
    except Exception:  # noqa: BLE001
        pass


def _load_cursor() -> int | None:
    try:
        return int(json.loads(CURSOR_FILE.read_text(encoding="utf-8"))["id"])
    except Exception:  # noqa: BLE001
        return None


def _save_cursor(v: int) -> None:
    try:
        CURSOR_FILE.parent.mkdir(parents=True, exist_ok=True)
        CURSOR_FILE.write_text(json.dumps({"id": int(v)}), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        # 写不进去 = 每轮都会从头重发（曾造成一次刷屏）。必须喊出来，不能静默。
        logger.error(f"[agent] 游标写入失败（会重复发消息！）：{type(e).__name__}: {e}")


async def _init_cursor() -> None:
    """首次启动对齐到最新一条，绝不重放历史。"""
    c = _load_cursor()
    if c is None:
        try:
            r = await _get("/outbox/head")
            c = int(r.get("max") or 0)
        except Exception:  # noqa: BLE001
            c = 0
        _save_cursor(c)
        logger.info(f"[agent] 初始化游标 {c}（不回放历史消息）")
    _cursor["id"] = c


async def _loop() -> None:
    await asyncio.sleep(10)          # 等 bot 连上，避免启动期刷错误
    await _init_cursor()
    while True:
        try:
            await _poll_once()
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[agent] 轮询异常：{type(e).__name__}")
        await asyncio.sleep(POLL_SECONDS)


@get_driver().on_startup
async def _start() -> None:
    asyncio.create_task(_loop())