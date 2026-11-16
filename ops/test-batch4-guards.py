#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""批次 4 护栏自检：审计链 / 授权门 / 多段语音 / 主动通道 / 配置一致性。

不依赖第三方库、不碰生产数据；正例之外注入反例，确认守门员会红。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
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
          and "proactive_channels as proactive_channels_mod" in smoke)


def main() -> None:
    voice_checks()
    audit_checks()
    channel_checks()
    wiring_checks()
    if FAILS:
        print(f"\n=== {TOTAL - len(FAILS)}/{TOTAL} 通过；失败：{', '.join(FAILS)} ===")
        sys.exit(1)
    print(f"\n=== {TOTAL}/{TOTAL} 通过 ===")


if __name__ == "__main__":
    main()
