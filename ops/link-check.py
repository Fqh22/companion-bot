"""跨层联动体检：真的把 plugins.chat 的 handler 跑起来，验证它和 app_core 的接线。

为什么需要这一层：离线回归只测 `app_core`（纯逻辑），`smoke-plugins` 只验"能导入"。
**两者之间的缝没人测** —— 插件把什么参数传给 core、返回什么、队列怎么攒、失败怎么回，
全靠真实 QQ 消息来发现。上一版的「图片绕过防抖」就是掉在这条缝里。

用法（在装了 nonebot 的环境里跑）：
    QQBOT_CODE_DIR=/opt/nonebot /opt/nonebot/venv/bin/python ops/link-check.py

覆盖：
  静态 1. 没有引用已删除的模块（merge）
       2. config.DEFAULTS 与 config.example.yaml 的键对齐（防漂移）
       3. app_core 不得 import nonebot（分层红线，与 lint 互为兜底）
  动态 4. 纯文本 → 一次回复，参数正确
       5. 图片 + 补一句（窗口内）→ 合并成一轮
       6. 只有图片（窗口内无补充）→ 占位正文
       7. 生成期间又来一条 → 排队，第二轮独立
       8. 命令 /xxx → 不触发回复
       9. 语音无文字 → 提示、不触发回复
      10. 图片下载失败 → 提示、不触发回复
"""
from __future__ import annotations

import ast
import asyncio
import os
import sys
import tempfile
from pathlib import Path

# Windows 控制台默认 GBK，打印 emoji / 生僻字会直接 UnicodeEncodeError 把测试打断。
# 放在这里是为了在任何输出之前生效；不支持 reconfigure 的环境静默跳过。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass
CODE = Path(os.environ.get("QQBOT_CODE_DIR", "/opt/nonebot")).resolve()
sys.path.insert(0, str(CODE))

PASS = FAIL = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"PASS {name}" + (f" | {extra}" if extra else ""))
    else:
        FAIL += 1
        print(f"FAIL {name}" + (f" | {extra}" if extra else ""))


# ---------------- 静态：跨模块引用 ----------------
def static_checks() -> None:
    core_dir = CODE / "app_core"
    plug_dir = CODE / "plugins"

    bad_import = []
    for p in plug_dir.rglob("*.py"):
        txt = p.read_text(encoding="utf-8", errors="replace")
        if "app_core import merge" in txt or "app_core.merge" in txt:
            bad_import.append(str(p.relative_to(CODE)))
    check("没有插件还在 import 已删除的 merge", not bad_import, ", ".join(bad_import))
    check("merge.py 确实已删除", not (core_dir / "merge.py").exists())

    # 所有命令都必须有权限门：群里任何人都不该能触发他的私聊命令
    # （2026-11-03 实测：21 条命令里 15 条没有 permission，/usage、/diary_show
    #   在群里对所有人开放。这是「群聊另写一套」之外、一直在的泄漏面。）
    naked = []
    for p in (CODE / "plugins").rglob("*.py"):
        try:
            tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = getattr(node.func, "id", getattr(node.func, "attr", ""))
            if fn in ("on_command", "on_startswith", "on_fullmatch", "on_regex"):
                kws = {k.arg for k in node.keywords}
                if "permission" not in kws and "rule" not in kws:
                    naked.append(f"{p.relative_to(CODE)}:{node.lineno}")
    check("所有命令都有权限门", not naked, ", ".join(naked)[:160])

    # 每个 app_core 模块都必须进 smoke-all：覆盖率体检靠它，「新模块忘了加」
    # 是 2026-11-03 真实踩过的坑（turn/group/watch/purge 三十个函数一整天在视野外）。
    smoke = None
    for cand in (CODE / "ops" / "smoke-all.py", CODE.parent / "ops" / "smoke-all.py",
                 CODE.parent.parent / "ops" / "smoke-all.py"):
        if cand.exists():
            smoke = cand
            break
    if smoke is None:
        print("SKIP 找不到 smoke-all.py，跳过「每个模块都进体检」检查")
    else:
        covered = set()
        for node in ast.walk(ast.parse(smoke.read_text(encoding="utf-8", errors="replace"))):
            if isinstance(node, ast.ImportFrom) and (node.module or "") == "app_core":
                covered.update(a.name for a in node.names)
            elif isinstance(node, ast.Import):
                for a in node.names:
                    if a.name.startswith("app_core."):
                        covered.add(a.name.split(".", 1)[1])
        mods = {p.stem for p in (CODE / "app_core").glob("*.py") if p.stem != "__init__"}
        missing_mods = sorted(mods - covered)
        check("app_core 每个模块都进了 smoke-all", not missing_mods,
              ", ".join(missing_mods))

    # 分层红线
    leaks = []
    for p in core_dir.rglob("*.py"):
        if "import nonebot" in p.read_text(encoding="utf-8", errors="replace"):
            leaks.append(str(p.relative_to(CODE)))
    check("app_core 不 import nonebot", not leaks, ", ".join(leaks))

    # 配置键漂移：DEFAULTS 里有的段，example.yaml 里也该有
    # （云端只部署 config.yaml，没有模板文件，那里跳过）
    from app_core import config
    ex_path = CODE / "config.example.yaml"
    if ex_path.exists():
        ex = ex_path.read_text(encoding="utf-8")
        missing = [k for k in config.DEFAULTS if f"\n{k}:" not in "\n" + ex]
        check("config.example.yaml 覆盖 DEFAULTS 的所有段", not missing, ", ".join(missing))
    else:
        print("SKIP config.example.yaml 不在部署目录（云端正常），跳过键对齐检查")


# ---------------- 动态：把 handler 真跑一遍 ----------------
def build_harness():
    """在临时目录里 init nonebot，再导入真正的插件模块。"""
    os.chdir(tempfile.mkdtemp(prefix="link-"))
    os.environ.setdefault("DRIVER", "~websockets")   # 临时目录里没有 .env，得显式指定
    import nonebot
    from nonebot.adapters.onebot.v11 import Adapter

    nonebot.init()
    nonebot.get_driver().register_adapter(Adapter)
    os.chdir(CODE)
    import plugins.chat as chat_mod

    return chat_mod


async def dynamic_checks(chat_mod) -> None:
    from nonebot.adapters.onebot.v11 import Message, MessageSegment, PrivateMessageEvent
    from app_core import pacing

    # 节奏会真的 sleep，测试里清零
    pacing.pace_seconds = lambda *a, **k: 0.0
    pacing.gap_seconds = lambda *a, **k: 0.0
    chat_mod.SETTLE_MEDIA = 0.25          # 缩短等待窗口，不然每个用例要 1.5 秒

    calls: list[dict] = []
    sent: list[str] = []

    class FakeChat:
        async def send(self, msg):
            sent.append(str(msg))

        async def finish(self, msg=None):
            if msg:
                sent.append(str(msg))

    chat_mod.chat = FakeChat()

    delay = {"v": 0.0}

    async def fake_reply(session, text, images=None, on_sentence=None, store_text=None, **kw):
        calls.append({"session": session, "text": text, "images": images,
                      "store": store_text})
        if delay["v"]:
            await asyncio.sleep(delay["v"])
        if on_sentence:
            await on_sentence("嗯。")
        return [], []

    chat_mod.core.reply = fake_reply
    chat_mod._bot = lambda: object()      # 没有真连接，给个非 None 让下载分支能被走到

    async def fake_image(bot, seg):
        return "/tmp/fake.jpg"

    async def fake_file(bot, seg):
        return Path("/tmp/fake.pdf"), "报表.pdf"

    def fake_parse(path):
        return "第一行\n第二行", "文本"

    chat_mod.save_image = fake_image
    chat_mod.save_from_segment = fake_file
    chat_mod.parse = fake_parse

    def ev(segs, text=""):
        msg = Message(segs) if segs else Message(text)
        return PrivateMessageEvent(
            time=0, self_id=10000, post_type="message", sub_type="friend",
            user_id=10001, message_type="private", message_id=1,
            message=msg, raw_message=text, font=0,
            sender={"user_id": 10001, "nickname": "x"}, to_me=True)

    sid = "private:10001"

    # 把每次起的泵收集下来，才能把异常打出来（否则任务失败只会静默消失）
    spawned: list[asyncio.Task] = []
    _orig_kick = chat_mod._kick

    def _kick_spy(s: str) -> None:
        _orig_kick(s)
        task = chat_mod._pump.get(s)
        if task is not None and task not in spawned:
            spawned.append(task)

    chat_mod._kick = _kick_spy

    async def drain():
        for _ in range(300):
            live = [t for t in spawned if not t.done()]
            if not live:
                break
            await asyncio.sleep(0.02)
        for t in spawned:
            if t.done() and t.exception() is not None:
                print("  [泵异常]", repr(t.exception()))
        spawned.clear()

    # 4. 纯文本
    calls.clear(); sent.clear()
    await chat_mod._handle(ev([], "在吗"))
    await drain()
    check("纯文本触发一次回复", len(calls) == 1 and calls[0]["text"] == "在吗",
          str(calls))
    check("纯文本立即开始（无等待）", len(calls) == 1)

    # 5. 图片 + 窗口内补一句
    calls.clear()
    await chat_mod._handle(ev([MessageSegment.image("https://x/a.jpg")]))
    await asyncio.sleep(0.05)
    await chat_mod._handle(ev([], "这是昨天的报错"))
    await drain()
    merged = calls[0] if calls else {}
    check("图 + 补一句合并成一轮", len(calls) == 1, f"{len(calls)} 次调用")
    check("合并后图片没丢", bool(merged.get("images")), str(merged.get("images")))
    check("合并后文字进了正文", "昨天的报错" in str(merged.get("text")), str(merged.get("text"))[:40])

    # 6. 只有图片
    calls.clear()
    await chat_mod._handle(ev([MessageSegment.image("https://x/b.jpg")]))
    await drain()
    check("只有图片时用占位正文", len(calls) == 1 and calls[0]["text"] == "[发了图片]",
          str(calls)[:80])

    # 7. 生成期间又来一条 → 排队，两轮
    calls.clear(); sent.clear()
    delay["v"] = 0.3
    await chat_mod._handle(ev([], "第一句"))
    await asyncio.sleep(0.05)
    await chat_mod._handle(ev([], "第二句"))
    await drain()
    delay["v"] = 0.0
    check("生成期间的消息不丢，排队成第二轮", len(calls) == 2, f"{len(calls)} 次")
    if len(calls) == 2:
        check("第二轮只带自己的内容", calls[1]["text"] == "第二句", calls[1]["text"])

    # 8. 命令
    calls.clear()
    await chat_mod._handle(ev([], "/ping"))
    await drain()
    check("命令不触发回复", not calls, str(calls))

    # 9. 语音（未接）
    calls.clear(); sent.clear()
    await chat_mod._handle(ev([MessageSegment("record", {"file": "a.amr"})]))
    await drain()
    check("语音给出提示且不回复", not calls and any("语音" in s for s in sent), str(sent))

    # 10. 文件 + 补一句
    calls.clear()
    await chat_mod._handle(ev([MessageSegment("file", {"file": "报表.pdf"})]))
    await asyncio.sleep(0.05)
    await chat_mod._handle(ev([], "帮我看下第一季度"))
    await drain()
    fcall = calls[0] if calls else {}
    check("文件 + 补一句合并成一轮", len(calls) == 1, f"{len(calls)} 次调用")
    check("文件正文进了 prompt", "报表.pdf" in str(fcall.get("text")),
          str(fcall.get("text"))[:50])
    check("补充说明也进了 prompt", "第一季度" in str(fcall.get("text")),
          str(fcall.get("text"))[-40:])
    check("入库摘要带补充说明", "第一季度" in str(fcall.get("store")),
          str(fcall.get("store"))[:60])

    # 11. 图片下载失败
    calls.clear(); sent.clear()

    async def bad_image(bot, seg):
        return None

    chat_mod.save_image = bad_image
    await chat_mod._handle(ev([MessageSegment.image("https://x/c.jpg")]))
    await drain()
    check("图片拿不到时给提示且不回复", not calls and any("图片" in s for s in sent), str(sent))
    chat_mod.save_image = fake_image

    # 12. 串链风险：私聊 handler 绝不能处理群消息
    #     一旦串了，群友说的话会进他的私有记忆库（这是本功能最严重的失败模式）
    from nonebot.adapters.onebot.v11 import GroupMessageEvent

    calls.clear(); sent.clear()
    group_ev = GroupMessageEvent(
        time=0, self_id=10000, post_type="message", sub_type="normal",
        user_id=10001, message_type="group", message_id=1,
        message=Message("在吗"), raw_message="在吗", font=0,
        sender={"user_id": 10001, "nickname": "x"}, to_me=True,
        group_id=222, anonymous=None)
    await chat_mod._handle(group_ev)
    await drain()
    check("私聊 handler 不处理群消息", not calls and not sent, str(calls))

    # 13. 连发多条：一条都不能丢（生成期到达的会被合并进下一轮，这是期望行为）
    calls.clear()
    delay["v"] = 0.05
    for i in range(5):
        await chat_mod._handle(ev([], f"第{i}句"))
        await asyncio.sleep(0.01)
    await drain()
    delay["v"] = 0.0
    all_text = " ".join(str(c.get("text")) for c in calls)
    lost = [i for i in range(5) if f"第{i}句" not in all_text]
    check("连发 5 条一条不丢（可合并成多轮）", not lost,
          f"{len(calls)} 轮，缺 {lost}")
    check("处理完泵表已清空（无泄漏）", not chat_mod._pump, str(chat_mod._pump))


# ---------------- 群聊：独立链路 ----------------
FORBIDDEN = ("app_core.memory", "app_core.mood", "app_core.chat", "app_core.followup",
             "app_core.remind", "app_core.diary", "app_core.proactive",
             "app_core.learn", "app_core.kb", "app_core.wiki")


def group_isolation() -> None:
    """在干净子进程里只导入 app_core.group，确认它没把记忆/情绪拖进来。"""
    import subprocess
    code = (
        "import sys\n"
        f"sys.path.insert(0, {str(CODE)!r})\n"
        "import app_core.group\n"
        f"bad = [m for m in {FORBIDDEN!r} if m in sys.modules]\n"
        "print(','.join(bad))\n")
    try:
        r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                           text=True, timeout=30)
        bad = (r.stdout or "").strip()
        check("群聊模块不牵连记忆/情绪/知识库", not bad, bad or "干净")
    except Exception as e:  # noqa: BLE001
        check("群聊模块不牵连记忆/情绪/知识库", False, f"{type(e).__name__}: {e}")


async def group_checks() -> None:
    import plugins.group as gmod
    from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message

    seen: list[tuple] = []

    async def fake_reply(gid, text):
        seen.append(("reply", gid, text))
        return ["好。"]

    class FakeChat:
        async def send(self, msg):
            seen.append(("send", str(msg)))

    gmod.core.reply = fake_reply
    gmod.grp = FakeChat()

    def gev(gid: int, text: str, to_me: bool):
        return GroupMessageEvent(
            time=0, self_id=10000, post_type="message", sub_type="normal",
            user_id=10001, message_type="group", message_id=1,
            message=Message(text), raw_message=text, font=0,
            sender={"user_id": 10001, "nickname": "x"}, to_me=to_me,
            group_id=gid, anonymous=None)

    def replies() -> list:
        return [s for s in seen if s[0] == "reply"]

    # 未启用
    gmod.core.enabled = False
    gmod._recent.clear(); seen.clear()
    await gmod._handle(gev(111, "在吗", True))
    check("群聊未启用时不回", not replies(), str(seen))

    # 启用但白名单为空 —— 空跑状态
    gmod.core.enabled = True
    gmod.core.whitelist = set()
    gmod.core.rate_per_minute = 3
    gmod.core.cooldown = 0
    gmod._recent.clear(); seen.clear()
    await gmod._handle(gev(111, "在吗", True))
    check("白名单为空时任何群都不回（空跑）", not replies(), str(seen))

    # 白名单命中 + 没 @
    gmod.core.whitelist = {"111"}
    gmod._recent.clear(); seen.clear()
    await gmod._handle(gev(111, "在吗", False))
    check("群里没 @ 时不回", not replies(), str(seen))

    # 白名单命中 + @
    gmod._recent.clear(); seen.clear()
    await gmod._handle(gev(111, "@我 在吗", True))
    check("@ 了才回", len(replies()) == 1 and replies()[0][1] == "111", str(seen))
    check("回复真的发出去了", any(s[0] == "send" for s in seen), str(seen))

    # 限流：每分钟 2 条
    gmod.core.rate_per_minute = 2
    gmod._recent.clear(); seen.clear()
    for _ in range(4):
        await gmod._handle(gev(111, "@我 再来", True))
    check("限流生效：4 条只回 2 条", len(replies()) == 2, f"{len(replies())} 次")

    # 失败静默：模型抛异常时群里有任何输出没有
    async def boom(gid, text):
        raise RuntimeError("模拟 401")

    gmod.core.reply = boom
    gmod.core.rate_per_minute = 5
    gmod._recent.clear(); seen.clear()
    await gmod._handle(gev(111, "@我 在吗", True))
    check("模型失败时群里静默", not seen, str(seen))

    # 反向串链：群 handler 不能处理私聊消息
    from nonebot.adapters.onebot.v11 import PrivateMessageEvent

    priv = PrivateMessageEvent(
        time=0, self_id=10000, post_type="message", sub_type="friend",
        user_id=10001, message_type="private", message_id=2,
        message=Message("在吗"), raw_message="在吗", font=0,
        sender={"user_id": 10001, "nickname": "x"}, to_me=True)
    seen.clear()
    await gmod._handle(priv)
    check("群 handler 不处理私聊消息", not seen, str(seen))


async def plugin_logic_checks() -> None:
    """插件里那些不经过 _handle 的逻辑：命令参数解析、后台图注任务。"""
    from types import SimpleNamespace

    import plugins.commands as cmd_mod

    ev = SimpleNamespace(get_plaintext=lambda: "/mood")
    check("commands._arg 去掉命令前缀", cmd_mod._arg(ev, "/mood") == "")
    ev2 = SimpleNamespace(get_plaintext=lambda: "/mood 周")
    check("commands._arg 保留参数", cmd_mod._arg(ev2, "/mood") == "周", cmd_mod._arg(ev2, "/mood"))

    # admin 的 _arg 是同一个契约：参数从 event 里剥命令前缀。
    # 这条是补的回归——上一版用 nonebot.get_current_event() 拿事件、又不剥前缀，
    # 于是 /profile_forget 永远只会回「用法：…」或「没有这个字段」，而且
    # 异常被 try/except 吞掉，日志里一个字都没有。纯逻辑层测不到，只有这里能测。
    import plugins.admin as admin_mod

    ev3 = SimpleNamespace(get_plaintext=lambda: "/profile_forget 职业与求职")
    check("admin._arg 剥掉命令前缀",
          admin_mod._arg(ev3, "/profile_forget") == "职业与求职",
          admin_mod._arg(ev3, "/profile_forget") if hasattr(admin_mod, "_arg") else "缺 _arg")
    ev4 = SimpleNamespace(get_plaintext=lambda: "/profile_forget")
    check("admin._arg 无参数时为空", admin_mod._arg(ev4, "/profile_forget") == "",
          admin_mod._arg(ev4, "/profile_forget") if hasattr(admin_mod, "_arg") else "缺 _arg")

    # 长推送：以前 text[:1200] 硬截，周报会停在半句上且没有任何提示
    import plugins.schedule as sched_mod

    check("短推送不分条", sched_mod._chunks("短消息") == ["短消息"])
    _parts = sched_mod._chunks("啊" * 5000, 1200, 3)
    check("超长推送按边界分条并说明剩余",
          len(_parts) == 4 and _parts[-1].startswith("…（还有"), f"{len(_parts)} 段")

    # agentd 发件箱：发送失败时不能推进游标（否则任务结果永久丢）
    import plugins.agent as agent_mod
    import plugins.chat as chat_mod2

    _saved = (agent_mod._get, agent_mod._post, agent_mod._say,
              agent_mod._append_history, agent_mod._save_cursor, agent_mod._owner,
              dict(agent_mod._cursor), dict(agent_mod._fail))
    _calls: list[tuple] = []

    async def _g(path, params=None):
        return {"messages": [{"id": 7, "text": "任务完成"}]}

    async def _p(path, data=None):
        _calls.append(("post", path))
        return {}

    async def _h(sid, text):
        _calls.append(("hist", text))

    async def _fail_send(text):
        _calls.append(("say", text))
        return False

    async def _ok_send(text):
        _calls.append(("say", text))
        return True

    # _owner 读的是 driver 配置，不是 core.admin_uid：测试里必须直接注入，
    # 否则两条用例都会在「管理员未配置」处早退，失败用例会假通过。
    agent_mod._owner = lambda: 10001
    agent_mod._get, agent_mod._post = _g, _p
    agent_mod._append_history = _h
    agent_mod._save_cursor = lambda v: _calls.append(("cursor", v))
    try:
        # 失败：游标不动、不 ack、不写历史
        agent_mod._say = _fail_send
        agent_mod._cursor["id"] = 0
        agent_mod._fail.update({"id": 0, "n": 0})
        _calls.clear()
        await agent_mod._poll_once()
        check("agentd 发送失败不推进游标", agent_mod._cursor["id"] == 0,
              str(agent_mod._cursor))
        check("agentd 发送失败不 ack",
              not any(c[0] == "post" and c[1] == "/outbox/ack" for c in _calls),
              str(_calls)[:80])
        check("agentd 发送失败不写历史",
              not any(c[0] == "hist" for c in _calls), str(_calls)[:80])

        # 成功：游标推进 + ack + 写历史
        agent_mod._say = _ok_send
        agent_mod._cursor["id"] = 0
        agent_mod._fail.update({"id": 0, "n": 0})
        _calls.clear()
        await agent_mod._poll_once()
        check("agentd 发送成功才推进游标", agent_mod._cursor["id"] == 7,
              str(agent_mod._cursor))
        check("agentd 发送成功会 ack",
              any(c[0] == "post" and c[1] == "/outbox/ack" for c in _calls),
              str(_calls)[:80])
        check("agentd 发送成功才写历史",
              any(c[0] == "hist" for c in _calls), str(_calls)[:80])
    finally:
        (agent_mod._get, agent_mod._post, agent_mod._say,
         agent_mod._append_history, agent_mod._save_cursor, agent_mod._owner,
         _c, _f) = _saved
        agent_mod._cursor.clear(); agent_mod._cursor.update(_c)
        agent_mod._fail.clear(); agent_mod._fail.update(_f)

    import plugins.chat as chat_mod2

    calls: list[tuple] = []

    async def fake_cap(paths, limit: int = 2):
        calls.append(("cap", list(paths)))
        return "一只猫"

    async def fake_append(mid, suffix):
        calls.append(("append", mid, suffix))
        return True

    _s1, _s2 = chat_mod2.core.caption_images, chat_mod2.core.append_to_message
    chat_mod2.core.caption_images = fake_cap
    chat_mod2.core.append_to_message = fake_append
    try:
        await chat_mod2._caption_later(42, ["/tmp/a.jpg"])
        check("后台图注任务会写回原消息",
              any(c[0] == "append" and c[1] == 42 and "图注" in c[2] for c in calls),
              str(calls))
        calls.clear()
        chat_mod2.core.caption_images = lambda paths, limit=2: _none()
        await chat_mod2._caption_later(43, ["/tmp/a.jpg"])
        check("图注为空时不写回", not calls, str(calls))
    finally:
        chat_mod2.core.caption_images = _s1
        chat_mod2.core.append_to_message = _s2


async def _none():
    return ""


def main() -> None:
    print("=" * 50)
    print("静态检查：跨模块引用 / 分层 / 配置漂移")
    print("=" * 50)
    static_checks()

    print("=" * 50)
    print("动态检查：真的把 plugins.chat 的 handler 跑一遍")
    print("=" * 50)
    chat_mod = build_harness()
    asyncio.run(dynamic_checks(chat_mod))

    print("=" * 50)
    print("群聊：与私聊彻底隔离的独立链路")
    print("=" * 50)
    group_isolation()
    asyncio.run(group_checks())

    print("=" * 50)
    print("插件逻辑：参数解析 / 后台任务")
    print("=" * 50)
    asyncio.run(plugin_logic_checks())

    print(f"\n=== 联动体检：{PASS}/{PASS + FAIL} 通过 ===")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
