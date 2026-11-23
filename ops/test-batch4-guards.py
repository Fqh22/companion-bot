#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""批次 4 护栏自检：审计链 / 授权门 / 多段语音 / 主动通道 / 配置一致性。

不依赖第三方库、不碰生产数据；正例之外注入反例，确认守门员会红。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CODE = Path(os.environ.get("QQBOT_CODE_DIR", str(ROOT / "nonebot"))).resolve()
sys.path.insert(0, str(CODE))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

from app_core import audit, pacing, proactive_channels  # noqa: E402

TOTAL = 0
FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    global TOTAL
    TOTAL += 1
    print(("PASS " if cond else "FAIL ") + name + (f" | {extra}" if extra else ""))
    if not cond:
        FAILS.append(name)


def read(path: Path, name: str) -> str:
    return (path / name).read_text(encoding="utf-8")


def voice_checks() -> None:
    check("多段语音：短文本原样一条",
          pacing.split_for_voice("今天还顺利吗？") == ["今天还顺利吗？"])
    check("多段语音：空文本不合成", pacing.split_for_voice("   ") == [])
    long = "".join(f"这是第{i}句话说给你听的内容。".replace("说给你听的内容", "说给你听的一句比较长的内容")
                   for i in range(1, 40))
    parts = pacing.split_for_voice(long)
    check("多段语音：长文案切成多段", len(parts) > 1, f"{len(parts)} 段")
    check("多段语音：每段都不超 300 字", all(len(p) <= 300 for p in parts))
    check("多段语音：段数有上限", len(parts) <= pacing.VOICE_MAX_PARTS)
    check("多段语音：内容不丢（拼接等于原文）", "".join(parts) == long)
    huge = "啊" * 5000
    hp = pacing.split_for_voice(huge)
    check("多段语音：超长单句硬切且有上限",
          len(hp) == pacing.VOICE_MAX_PARTS and all(len(p) <= 300 for p in hp))


def audit_checks() -> None:
    with tempfile.TemporaryDirectory(prefix="audit-guard-") as td:
        root = Path(td)
        log, pending = root / "audit.jsonl", root / "pending.json"
        for name, status in (("note_write", "done"),
                             ("remind_me", "done"), ("meme_send", "done")):
            audit.record(name, target="t", status=status, path=log)
        rows = audit.recent(8, path=log)
        ok, errors = audit.verify_chain(rows)
        check("审计：三条记录哈希链完整", len(rows) == 3 and ok, "; ".join(errors))
        check("审计：发出去的消息标不可回滚",
              rows[-1]["revertible"] is False and rows[0]["revertible"] is True)
        # 反例：篡改中间一行，链必须断
        raw = log.read_text(encoding="utf-8").splitlines()
        obj = json.loads(raw[1]); obj["target"] = "被改过"
        raw[1] = json.dumps(obj, ensure_ascii=False, sort_keys=True)
        log.write_text("\n".join(raw) + "\n", encoding="utf-8")
        ok2, errors2 = audit.verify_chain(audit.recent(8, path=log))
        check("审计：篡改中间记录会被链校验抓到", not ok2 and bool(errors2))

        rid = audit.propose("note_write", {"title": "笔记A"}, session="s1",
                            pending_path=pending, log_path=log)
        check("审计：待授权登记后能查到", rid in audit.pending_ids(pending))
        item = audit.decide(rid, True, by="owner", pending_path=pending, log_path=log)
        check("审计：批准返回原请求供重放",
              item is not None and item["name"] == "note_write"
              and item["args"] == {"title": "笔记A"})
        check("审计：同一请求不能批准两次",
              audit.decide(rid, True, pending_path=pending, log_path=log) is None)
        rid2 = audit.propose("remind_me", {"text": "开会"}, pending_path=pending,
                             log_path=log)
        check("审计：拒绝不返回可执行请求",
              audit.decide(rid2, False, pending_path=pending, log_path=log) is None)
        statuses = [r["status"] for r in audit.recent(8, path=log)]
        check("审计：状态追加成时间线，不覆盖",
              "pending" in statuses and "approved" in statuses
              and "denied" in statuses, str(statuses))
        p = json.loads(pending.read_text(encoding="utf-8"))
        p["old"] = {"ts": 1.0, "name": "note_write", "args": {}, "session": ""}
        pending.write_text(json.dumps(p, ensure_ascii=False), encoding="utf-8")
        check("审计：过期请求不执行",
              audit.decide("old", True, pending_path=pending, log_path=log) is None)

    check("授权门：默认关闭，旧行为不变",
          audit.requires_approval("note_write", cfg={"tools": {}}) is False)
    check("授权门：开启后只拦写类工具",
          audit.requires_approval("note_write", cfg={"tools": {"require_approval": True}})
          and not audit.requires_approval("kb_read", cfg={"tools": {"require_approval": True}}))

    # 故障注入：审批系统坏掉时，写工具必须**不执行**（不能吞异常继续写）。
    from app_core import tools

    def _boom(*_a, **_k):
        raise OSError("disk full")

    called: list[int] = []

    async def _fake_handler(core, args, images):
        called.append(1)
        return "写了"

    _old_handler = tools.HANDLERS["note_write"]
    _old_propose, _old_requires = audit.propose, audit.requires_approval
    tools.HANDLERS["note_write"] = _fake_handler
    try:
        audit.requires_approval = lambda name, cfg=None: True
        audit.propose = _boom
        out = asyncio.run(tools.execute(None, "note_write", {"title": "x"}))
        check("授权登记失败 → 写工具一次都没执行", called == [] and "没有执行" in out, out)

        with tempfile.TemporaryDirectory(prefix="audit-claim-") as td2:
            q = Path(td2)
            pending2, log2 = q / "pending.json", q / "audit.jsonl"
            pending2.write_text(json.dumps({
                "rid": {"ts": time.time(), "name": "note_write",
                        "args": {}, "session": ""}}), encoding="utf-8")
            _old_write = audit._write
            audit._write = _boom
            try:
                got = audit.decide("rid", True, pending_path=pending2, log_path=log2)
            finally:
                audit._write = _old_write
            check("领取落盘失败 → 不返回可执行请求", got is None)
            # 请求仍在盘上，但上一次没有执行；恢复后可以正常批准一次。
            got2 = audit.decide("rid", True, pending_path=pending2, log_path=log2)
            check("落盘恢复后只能批准一次",
                  got2 is not None and audit.decide(
                      "rid", True, pending_path=pending2, log_path=log2) is None)
    finally:
        tools.HANDLERS["note_write"] = _old_handler
        audit.propose, audit.requires_approval = _old_propose, _old_requires


def dedupe_checks() -> None:
    from app_core.seen import BoundedSeen

    seen = BoundedSeen(3)
    check("有界去重：新 ID 返回 True", seen.add("a") and seen.add("b") and seen.add("c"))
    check("有界去重：重复 ID 返回 False", not seen.add("a"))
    # 第 4 个进来时淘汰最旧的 b；a 刚被访问过，应该留下。
    seen.add("d")
    check("有界去重：容量边界只淘汰最旧，不整体清空",
          "a" in seen and "c" in seen and "d" in seen and "b" not in seen,
          str(list(seen._items)))
    check("有界去重：边界之后的重复仍被拦住",
          not seen.add("a") and not seen.add("d"))
    check("有界去重：空 ID 不参与去重", seen.add(""))


def outbound_checks() -> None:
    from app_core import outbound

    class _Audio:
        def unlink(self, missing_ok: bool = False) -> None:
            return None

    def run_case(voice_ok: bool, text_ok: bool, mode: str = "voice"):
        recorded: list[tuple[str, str]] = []
        texts: list[str] = []

        async def synth(seg):
            return _Audio()

        async def send_voice(path):
            return voice_ok

        async def send_text(text):
            texts.append(text)
            return text_ok

        async def record(text, kind):
            recorded.append((text, kind))

        ok = asyncio.run(outbound.deliver_voice_reply(
            buffered=["你好。", "再见。"], synth=synth, send_voice=send_voice,
            send_text=send_text, record=record, mode=mode))
        return ok, recorded, texts

    ok, rec, texts = run_case(False, False)
    check("语音/文字都失败 → 不写历史", not ok and rec == [], str(rec))
    ok, rec, texts = run_case(True, False)
    check("语音成功 → 历史写实际正文",
          ok and rec == [("你好。再见。", "voice")] and texts == [], str(rec))
    ok, rec, texts = run_case(False, True)
    check("语音失败、文字兜底成功 → 历史写实际正文",
          ok and rec == [("你好。再见。", "voice")] and len(texts) == 2, str(rec))
    ok, rec, texts = run_case(True, True, mode="voice_then_text")
    check("voice_then_text 不重复写历史",
          ok and len(rec) == 1, f"rec={rec} texts={texts}")

    # 前一段合成成功、后一段失败：不能把没送到的结尾写进历史，也不能整段算成功。
    long = ("甲" * 300) + ("乙" * 10)
    recorded: list[tuple[str, str]] = []
    texts: list[str] = []
    voices: list[str] = []

    async def synth_partial(seg):
        if seg.startswith("乙"):
            return None
        return _Audio()

    async def send_voice_ok(path):
        voices.append(path)
        return True

    async def send_text_fail(text):
        texts.append(text)
        return False

    async def record_partial(text, kind):
        recorded.append((text, kind))

    ok = asyncio.run(outbound.deliver_voice_reply(
        buffered=[long], synth=synth_partial, send_voice=send_voice_ok,
        send_text=send_text_fail, record=record_partial, mode="voice"))
    spoken = recorded[0][0] if recorded else ""
    check("多段语音失败段不进历史",
          not ok and spoken == "甲" * 300 and "乙" not in spoken and len(texts) == 1,
          f"ok={ok} rec={spoken[-20:]!r} texts={len(texts)}")

    # 恰好 2400 字（8×300）可以只发语音；多一个字必须有文字出口，不能静默丢尾。
    exact = "甲" * 2400
    over = exact + "乙乙"
    texts2: list[str] = []

    async def synth_ok(seg):
        return _Audio()

    async def voice_ok(path):
        return True

    async def text_catch(text):
        texts2.append(text)
        return True

    ok_exact = asyncio.run(outbound.deliver_voice_reply(
        buffered=[exact], synth=synth_ok, send_voice=voice_ok,
        send_text=text_catch, record=record_partial, mode="voice"))
    check("语音刚好到上限不额外补文字", ok_exact and texts2 == [], str(texts2)[:40])
    ok_over = asyncio.run(outbound.deliver_voice_reply(
        buffered=[over], synth=synth_ok, send_voice=voice_ok,
        send_text=text_catch, record=record_partial, mode="voice"))
    check("语音超出上限的尾巴有文字出口",
          ok_over and any("乙" in t for t in texts2),
          str(texts2))


def buffered_send_checks() -> None:
    """语音缓冲返回真值，引擎不要把同一句再发一遍；历史也不在这里写。"""
    from app_core.delivery import BUFFERED
    from app_core.engine import InboundMessage, OutboundMessage
    from app_core.runner import TurnRunner

    class _Ch:
        forms = {"text"}

        def __init__(self) -> None:
            self.buffered: list[str] = []
            self.calls = 0

        async def send_part(self, session, part):
            self.calls += 1
            self.buffered.append(part.content)
            return BUFFERED

        async def typing(self, session, on):
            return None

    class _Eng:
        core = None

        async def handle(self, msg, on_sentence=None, **kw):
            accepted = False
            if on_sentence:
                accepted = bool(await on_sentence("hello."))
            # 缓冲被当成失败时，引擎会把同一句再交回来。
            return OutboundMessage(chunks=[] if accepted else ["hello."])

    ch = _Ch()
    out = asyncio.run(TurnRunner(_Eng(), ch, deadline=5, stall_seconds=5).run(
        InboundMessage(session="private:1", text="hi")))
    check("已缓冲不算失败，补发尾巴不再进缓冲",
          ch.buffered == ["hello."] and ch.calls == 1, str(ch.buffered))
    check("已缓冲不写历史", out is not None and ch.calls == 1)


def channel_checks() -> None:
    seen: list[str] = []

    async def ok_sender(text: str) -> bool:
        seen.append(text)
        return True

    async def bad_sender(text: str) -> bool:
        raise RuntimeError("boom")

    import asyncio

    proactive_channels.unregister("t1"); proactive_channels.unregister("t2")
    proactive_channels.register("t1", ok_sender)
    proactive_channels.register("t2", bad_sender)
    check("主动通道：注册后可见", proactive_channels.available() == ["t1", "t2"])
    check("主动通道：按名字发送",
          asyncio.run(proactive_channels.send("你好", "t1")) and seen == ["你好"])
    check("主动通道：未注册返回 False（调用方可回退）",
          not asyncio.run(proactive_channels.send("x", "qq")))
    check("主动通道：发送抛异常也返回 False，不炸调度",
          not asyncio.run(proactive_channels.send("x", "t2")))
    proactive_channels.unregister("t1"); proactive_channels.unregister("t2")


def wiring_checks() -> None:
    core = CODE / "app_core"
    tools = read(core, "tools.py")
    loop = read(core, "tool_loop.py")
    cfg = read(core, "config.py")
    ex = read(CODE, "config.example.yaml")
    ui = read(ROOT / "ops", "config-ui.py")
    qq = read(CODE / "plugins" / "chat", "__init__.py")
    fs = read(CODE / "plugins" / "feishu", "__init__.py")
    pro = read(CODE / "plugins" / "proactive", "__init__.py")
    admin = read(CODE / "plugins" / "admin", "__init__.py")
    hp = read(core, "help.py")
    smoke = read(ROOT / "ops", "smoke-all.py")

    check("web_open 不再被当只读（两处名单）",
          '"web_open", "kb_read"' not in tools and '"web_open", "kb_read"' not in loop
          and '"web_search", "kb_read"' in tools)
    check("tools.execute 接上审计与授权重放入口",
          "audit.propose" in tools and "approved: bool = False" in tools
          and "audit.record" in tools)
    check("配置四处同步（DEFAULTS / example / 面板）",
          '"require_approval": False' in cfg and '"proactive": {' in cfg
          and "require_approval: false" in ex and "channel: \"qq\"" in ex
          and '"tools.require_approval"' in ui and '"proactive.channel"' in ui)
    # 2026-11-16：分段逻辑上移到 outbound.py，插件只传回调
    ob = read(core, "outbound.py")
    check("QQ / 飞书语音都走按句分段（outbound 统一）",
          ob.count("split_for_voice") >= 2
          and "outbound.speak_later" in qq and "outbound.speak_later" in fs)
    check("飞书主动发送 + 会话映射 + 注册都接上",
          "_send_proactive" in fs and "_save_chat_map" in fs
          and "proactive_channels.register" in fs)
    check("调度侧默认走 QQ，飞书不可用会回退",
          '"channel", "qq"' in pro and "回退 QQ" in pro)
    check("审计命令带 SUPERUSER 权限门并登记 help",
          'on_command("approve", priority=5, block=True, permission=SUPERUSER)' in admin
          and 'on_command("audit", priority=5, block=True, permission=SUPERUSER)' in admin
          and '("approve",' in hp and '("audit",' in hp)
    check("新 app_core 模块已进 smoke-all 覆盖",
          "audit as audit_mod" in smoke
          and "proactive_channels as proactive_channels_mod" in smoke
          and "inflight as inflight_mod" in smoke)
    check("语音缓冲不是发送失败",
          "return BUFFERED" in qq and "return BUFFERED" in fs)
    sync_ex = (ROOT / "ops" / "sync-config.example.json").read_text(encoding="utf-8")
    sync_ps = (ROOT / "ops" / "sync-from-cloud.ps1").read_text(encoding="utf-8")
    check("同步模板不把含密钥备份拉进共享工作区",
          "Documents/ai" not in sync_ex and "备份/云端" not in sync_ex
          and "Test-BackupDirAllowed" in sync_ps and ".partial-" in sync_ps)
    release = (ROOT / "ops" / "release-switch.sh").read_text(encoding="utf-8")
    check("发布脚本读取在途轮次", "inflight.json" in release)


def main() -> None:
    voice_checks()
    audit_checks()
    dedupe_checks()
    outbound_checks()
    buffered_send_checks()
    channel_checks()
    wiring_checks()
    if FAILS:
        print(f"\n=== {TOTAL - len(FAILS)}/{TOTAL} 通过；失败：{', '.join(FAILS)} ===")
        sys.exit(1)
    print(f"\n=== {TOTAL}/{TOTAL} 通过 ===")


if __name__ == "__main__":
    main()
