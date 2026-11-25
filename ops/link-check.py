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
import re
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

# Windows 控制台默认 GBK，打印 emoji / 生僻字会直接 UnicodeEncodeError 把测试打断。
# 放在这里是为了在任何输出之前生效；不支持 reconfigure 的环境静默跳过。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass
CODE = Path(os.environ.get("QQBOT_CODE_DIR", "/opt/nonebot")).resolve()
sys.path.insert(0, str(CODE))

# paths.py 在 import 时固化 QQBOT_ROOT：必须赶在 import app_core / plugins 之前隔离，
# 否则插件里走默认路径的保存/日志会写 /opt —— 云端就是生产库。
TMP = Path(tempfile.mkdtemp(prefix="link-"))
os.environ.setdefault("QQBOT_ROOT", str(TMP))

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

    import_cycle_checks()


# 文档链接 / 路径引用 / 登记表一致性统一由 ops/check-links.py 负责（CI lint job）。
# 2026-11-16 移除这里的重复实现：云端部署目录没有 docs 时它只会 SKIP，
# 本地又与 check-links.py 跑同一件事、判定规则还不一致，留着只会给出第二个口径。
# 本文件只管插件 ↔ 内核的代码接线。


# ---------------- 静态：import 环 ----------------
# 已存在的环是历史结构。2026-11-16 重构后只剩 learn↔review 一组：
#   - learn↔wiki：抽 notes_meta 后消失
#   - mood/followup/remind/scheduler 四组：路径归 store 后消失
# 与 ops/lint-layers.py 的 CYCLE_BASELINE 保持同源；两边都只许减。
_ALLOWED_CYCLES: set[frozenset[str]] = set()  # 2026-11-16 后 app_core 无环


def import_cycle_checks() -> None:
    """app_core 内部不能出现新的 import 环，环内也不能双向顶层 import。

    环本身不一定炸（函数内 import 能绕），但双向**顶层** import 一定炸。
    这条检查是给未来的人看的：加一行顶层 import 就会红，不用等线上
    ImportError。
    """
    mods = {}
    for p in (CODE / "app_core").rglob("*.py"):
        if "__pycache__" in p.parts:
            continue
        rel = p.relative_to(CODE).with_suffix("")
        name = ".".join(rel.parts)
        if name.endswith(".__init__"):
            name = name[:-9]
        mods[name] = p

    def edges(p, top_only=False):
        tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"))
        nodes = tree.body if top_only else ast.walk(tree)
        out = set()
        for n in nodes:
            if isinstance(n, ast.ImportFrom) and n.module and n.module.startswith("app_core"):
                out.add(n.module)
                for a in n.names:
                    cand = n.module + "." + a.name
                    if cand in mods:
                        out.add(cand)
        return {x for x in out if x in mods}

    # 结构环只算**顶层** import：函数内 import 是显式的运行时解耦
    # （chat.__init__ 里 import plugin_api 就是这样），它不会在 import 顺序上炸，
    # 和「环内双向顶层 import」是两回事——后者由下面单独检查。
    graph = {m: edges(p, top_only=True) for m, p in mods.items()}
    top = graph

    index, low, stack, on, sccs = {}, {}, [], set(), []
    counter = [0]

    def strong(v):
        index[v] = low[v] = counter[0]
        counter[0] += 1
        stack.append(v)
        on.add(v)
        for w in graph.get(v, ()):
            if w not in index:
                strong(w)
                low[v] = min(low[v], low[w])
            elif w in on:
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            comp = []
            while True:
                w = stack.pop()
                on.discard(w)
                comp.append(w)
                if w == v:
                    break
            if len(comp) > 1:
                sccs.append(frozenset(comp))

    for v in graph:
        if v not in index:
            strong(v)

    new_cycles = [sorted(c) for c in sccs if c not in _ALLOWED_CYCLES]
    check("没有新的 import 环", not new_cycles, str(new_cycles)[:200])

    twins = []
    for c in sccs:
        for u in c:
            for v in top.get(u, ()):
                if v in c and v != u and u in top.get(v, ()):
                    twins.append(f"{u} <-> {v}")
    check("环内没有双向顶层 import", not twins, "; ".join(twins))


# ---------------- 动态：把 handler 真跑一遍 ----------------
def build_harness():
    """在临时目录里 init nonebot，再导入真正的插件模块。"""
    os.chdir(TMP)
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

    # 这些用例测的是主人私聊链路：把 10001 配成主人。否则凌晨跑测试时
    # 会被「非主人 0-8 点不回复」挡掉，失败原因和被测接线完全无关。
    chat_mod.core.admin_uid = "10001"
    chat_mod.core.admin_ids = []
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

    def ev(segs, text="", mid=1, reply=None):
        msg = Message(segs) if segs else Message(text)
        return PrivateMessageEvent(
            time=0, self_id=10000, post_type="message", sub_type="friend",
            user_id=10001, message_type="private", message_id=mid, reply=reply,
            message=msg, raw_message=text, font=0,
            sender={"user_id": 10001, "nickname": "x"}, to_me=True)

    spawned = []
    submit = chat_mod._turns.submit
    chat_mod._turns.text_gap = 0.08
    chat_mod._turns.media_gap = 0.12

    def capture(*args, **kwargs):
        task = submit(*args, **kwargs)
        if task is not None and task not in spawned:
            spawned.append(task)
        return task

    chat_mod._turns.submit = capture

    async def drain():
        if spawned:
            await asyncio.wait_for(asyncio.gather(*spawned), 6)
            spawned.clear()

    # 4. 纯文本
    calls.clear(); sent.clear()
    await chat_mod._handle(ev([], "在吗"))
    await drain()
    check("纯文本触发一次回复", len(calls) == 1 and calls[0]["text"] == "在吗",
          str(calls))
    check("默认不挂引用框", bool(sent) and "[CQ:reply" not in sent[0], str(sent[:1])[:80])
    # 开关打开时才引用：配置 chat.quote_reply
    chat_mod.QUOTE_REPLY = True
    calls.clear(); sent.clear()
    await chat_mod._handle(ev([], "在吗"))
    await drain()
    check("开启后第一段引用他的消息",
          bool(sent) and "[CQ:reply,id=1]" in sent[0], str(sent[:1])[:80])
    chat_mod.QUOTE_REPLY = False

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

    calls.clear()
    release_picture = asyncio.Event()

    async def slow_picture(bot, seg):
        await release_picture.wait()
        return "/tmp/slow.jpg"

    chat_mod.save_image = slow_picture
    await chat_mod._handle(ev([], "看我发的图"))
    await chat_mod._handle(ev([MessageSegment.image("https://x/slow.jpg")], mid=12))
    await asyncio.sleep(0.16)
    waiting = not calls
    release_picture.set()
    await drain()
    check("QQ 先文字，图片下载较慢时仍一起回答",
          waiting and len(calls) == 1 and calls[0]["text"] == "看我发的图"
          and calls[0]["images"] == ["/tmp/slow.jpg"])
    calls.clear()
    reply = {"message_id": 12, "real_id": 12, "message_type": "private",
             "time": 0, "sender": {"user_id": 10001},
             "message": Message([MessageSegment.image("https://x/slow.jpg")])}
    await chat_mod._handle(ev([], "红框在哪", mid=13, reply=reply))
    await drain()
    check("QQ 引用之前的图片按消息 ID 取回原图",
          len(calls) == 1 and calls[0]["images"] == ["/tmp/slow.jpg"])
    chat_mod.save_image = fake_image

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
    await asyncio.sleep(0.13)
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

    # 13. 连发多条：实际 handler 只调用引擎一次。
    calls.clear(); sent.clear()
    for i in range(5):
        await chat_mod._handle(ev([], f"第{i}句"))
        await asyncio.sleep(0.01)
    await drain()
    check("QQ 连发五条只回复一轮，原文依次保留",
          [c["text"] for c in calls] == ["\n".join(f"第{i}句" for i in range(5))]
          and len(sent) == 1, repr(calls))
    check("QQ 批次处理完成后清空会话", not chat_mod._turns._sessions)


async def feishu_checks():
    from unittest.mock import patch
    from plugins import feishu

    class Runner:
        def __init__(self, *args, **kwargs):
            pass

        async def run(self, inbound):
            calls.append(inbound)
            return SimpleNamespace()

    def event(mid, text, parent_id=None, message_type="text"):
        import json
        message = SimpleNamespace(chat_type="p2p", message_type=message_type, message_id=mid,
                                  chat_id="chat", parent_id=parent_id,
                                  content=json.dumps(text if isinstance(text, dict) else {"text": text}))
        sender = SimpleNamespace(sender_id=SimpleNamespace(open_id="owner"))
        return SimpleNamespace(event=SimpleNamespace(message=message, sender=sender))

    calls = []
    with patch.object(feishu, "OWNER", "owner"), patch.object(feishu, "TurnRunner", Runner):
        await feishu._handle(event("burst-1", "那件事先等等", "asked"))
        await feishu._handle(event("burst-2", "下周一再说"))
        await feishu._handle(event("burst-2", "重复事件"))
        await asyncio.wait_for(asyncio.gather(*(s.task for s in feishu._turns._sessions.values())), 6)
    check("飞书连发只进入一次 Runner，保留引用且过滤重复事件",
          len(calls) == 1 and calls[0].text == "那件事先等等\n下周一再说"
          and calls[0].reply_to == "asked" and calls[0].event_id == "burst-2")


    async def drain():
        await asyncio.wait_for(asyncio.gather(*(s.task for s in feishu._turns._sessions.values())), 6)

    calls.clear()
    release_image = asyncio.Event()

    async def load_image(mid, key):
        await release_image.wait()
        return "/tmp/feishu-image.jpg"

    with (patch.object(feishu, "OWNER", "owner"), patch.object(feishu, "TurnRunner", Runner),
          patch.object(feishu, "_load_image", load_image),
          patch.object(feishu._turns, "media_gap", 0.05), patch.object(feishu._turns, "text_gap", 0.05)):
        await feishu._handle(event("image-1", {"image_key": "key"}, message_type="image"))
        await feishu._handle(event("image-caption", "帮我看这张图"))
        await asyncio.sleep(0.12)
        waiting = not calls
        release_image.set()
        await drain()
        check("飞书先发图，图下载较慢时仍和后发说明一起进 Runner",
              waiting and len(calls) == 1 and calls[0].images == ["/tmp/feishu-image.jpg"]
              and calls[0].text == "帮我看这张图")
        calls.clear()
        await feishu._handle(event("image-reference", "红框怎么看", "image-1"))
        await drain()
        check("飞书引用跨轮可取回指定原图",
              len(calls) == 1 and calls[0].reply_to == "image-1"
              and calls[0].images == ["/tmp/feishu-image.jpg"])

        calls.clear()
        post = {"content": [[{"tag": "text", "text": "这张图怎么读"},
                             {"tag": "img", "image_key": "in-post"}]]}
        await feishu._handle(event("image-in-post", post, message_type="post"))
        await drain()
        check("飞书富文本同条图文一起处理",
              len(calls) == 1 and calls[0].text == "这张图怎么读"
              and calls[0].images == ["/tmp/feishu-image.jpg"])


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
    """单独验证命令参数解析与内核图注入库。"""
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

    import plugins.chat as chat_mod2
    from app_core.chat_turn_base import Turn

    calls: list[tuple] = []

    async def fake_append(mid, suffix):
        calls.append((mid, suffix))
        return True

    core = chat_mod2.core
    original = core.append_to_message
    core.append_to_message = fake_append
    try:
        turn = Turn("private:link-check", "看图", [], None, True, "test", 0.0,
                    user_msg_id=42, stored="看图", caption_text="一只猫")
        await core._commit_write_user(turn)
        check("内核图注只写回原消息", calls == [(42, "｜图注：一只猫")], str(calls))
        calls.clear()
        turn.caption_text = ""
        await core._commit_write_user(turn)
        check("图注为空时不写回", not calls, str(calls))
    finally:
        core.append_to_message = original


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
    asyncio.run(feishu_checks())

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
