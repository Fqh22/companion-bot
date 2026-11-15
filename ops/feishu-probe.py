#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""feishu-probe.py —— 飞书通道最小回环探针（不依赖 nonebot / app_core）。

接进内核之前先证明三件事：长连接能收到私聊、能拿到 sender open_id、能主动回一条文本。
它不写生产数据、只打印 + 回一句，用来验证「应用配置 + 权限 + 订阅」是否真的通了。

前置（飞书开放平台，一次性，约 10 分钟）：
  1. https://open.feishu.cn/app → 创建「企业自建应用」，添加「机器人」能力
  2. 权限管理：im:message、im:message:send_as_bot、im:resource、im:chat
  3. 事件与回调 → 事件配置 → 订阅方式选「使用长连接接收事件」→ 添加 im.message.receive_v1
  4. 版本管理与发布 → 创建版本 → 可用范围「仅自己」→ 发布（自建应用一般立即通过）
  5. 飞书客户端搜应用名 → 打开机器人私聊

凭证（不进仓库）：
  export FEISHU_APP_ID=cli_xxx FEISHU_APP_SECRET=xxx
  或写 /opt/qqbot-data/keys.json：{"feishu": {"app_id": "...", "app_secret": "..."}}

运行（云端用独立 venv，别装进 nonebot 的 venv）：
  /opt/feishu-probe-venv/bin/python ops/feishu-probe.py           # 常驻收消息
  /opt/feishu-probe-venv/bin/python ops/feishu-probe.py --check   # 只校验凭证格式，不建连
  nohup /opt/feishu-probe-venv/bin/python ops/feishu-probe.py > /tmp/feishu-probe.log 2>&1 &

安全：不打印 secret；只回复私聊（chat_type=p2p），群消息只记录不回复。
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

KEYS = Path("/opt/qqbot-data/keys.json")
APP_ID = ""
APP_SECRET = ""


def log(msg: str) -> None:
    print("[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg), flush=True)


def load_creds() -> tuple[str, str]:
    app_id = os.environ.get("FEISHU_APP_ID", "").strip()
    secret = os.environ.get("FEISHU_APP_SECRET", "").strip()
    if app_id and secret:
        return app_id, secret
    try:
        data = json.loads(KEYS.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    feishu = data.get("feishu") if isinstance(data.get("feishu"), dict) else {}
    app_id = app_id or str(feishu.get("app_id") or data.get("feishu_app_id") or "")
    secret = secret or str(feishu.get("app_secret") or data.get("feishu_app_secret") or "")
    return app_id.strip(), secret.strip()


def parse_event(data) -> tuple[str, str, str, str, str, str]:
    """事件 → (sender_open_id, chat_id, chat_type, msg_type, 可读文本, message_id)."""
    msg = data.event.message
    sender = data.event.sender
    open_id = ""
    try:
        open_id = str(sender.sender_id.open_id or "")
    except Exception:  # noqa: BLE001
        pass
    mtype = str(msg.message_type or "")
    try:
        obj = json.loads(msg.content or "{}")
    except Exception:  # noqa: BLE001
        obj = {}
    if mtype == "text":
        text = str(obj.get("text") or "")
    elif mtype == "image":
        text = "[图片]"
    elif mtype in ("audio", "media"):
        text = "[语音/媒体]"
    else:
        text = "[%s]" % mtype
    return (open_id, str(msg.chat_id or ""), str(msg.chat_type or ""),
            mtype, text, str(msg.message_id or ""))


def send_text(open_id: str, text: str) -> bool:
    import lark_oapi as lark
    from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

    client = lark.Client.builder().app_id(APP_ID).app_secret(APP_SECRET).build()
    body = (CreateMessageRequestBody.builder()
            .receive_id(open_id)
            .msg_type("text")
            .content(json.dumps({"text": text}, ensure_ascii=False))
            .build())
    req = (CreateMessageRequest.builder()
           .receive_id_type("open_id")
           .request_body(body)
           .build())
    resp = client.im.v1.message.create(req)
    if getattr(resp, "code", None) == 0:
        return True
    log("发送失败 code=%s msg=%s" % (getattr(resp, "code", "?"), getattr(resp, "msg", "?")))
    return False


_SEEN: set[str] = set()


def on_message(data) -> None:
    """事件回调必须快速返回（飞书要求 3 秒内处理完），真正的处理扔线程。"""
    def work() -> None:
        try:
            open_id, chat_id, chat_type, mtype, text, mid = parse_event(data)
            if mid and mid in _SEEN:
                return
            if mid:
                _SEEN.add(mid)
                if len(_SEEN) > 500:
                    _SEEN.clear()
            log("recv chat_type=%s chat=%s type=%s open_id=%s text=%r"
                % (chat_type, chat_id[:14], mtype, open_id or "-", text[:120]))
            if not open_id:
                log("没有 sender.open_id，跳过回复")
                return
            if chat_type != "p2p":
                log("非私聊（%s），探针只验证私聊，不回" % chat_type)
                return
            if send_text(open_id, "探针收到：" + text):
                log("已回复 open_id=%s" % open_id)
        except Exception as e:  # noqa: BLE001
            log("处理失败：%r" % (e,))

    threading.Thread(target=work, daemon=True).start()


def main() -> int:
    global APP_ID, APP_SECRET
    APP_ID, APP_SECRET = load_creds()
    if not APP_ID or not APP_SECRET:
        log("缺少凭证：export FEISHU_APP_ID/FEISHU_APP_SECRET，"
            "或写 %s 的 feishu.app_id / feishu.app_secret" % KEYS)
        return 2
    log("app_id=%s（secret 已加载，不打印）" % APP_ID)
    if "--check" in sys.argv:
        log("凭证格式检查通过（未建连）")
        return 0

    import lark_oapi as lark

    handler = (lark.EventDispatcherHandler.builder("", "")
               .register_p2_im_message_receive_v1(on_message)
               .build())
    cli = lark.ws.Client(APP_ID, APP_SECRET, event_handler=handler,
                         log_level=lark.LogLevel.INFO)
    log("建立长连接…（Ctrl-C 退出）")
    cli.start()
    return 0


if __name__ == "__main__":
    sys.exit(main())
