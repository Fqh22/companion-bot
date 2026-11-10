#!/usr/bin/env python3
"""QQ 入站归一化：私聊/群聊/元数据。"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CODE = HERE.parent / "nonebot"
if not (CODE / "app_core").is_dir():
    CODE = HERE.parent
sys.path.insert(0, str(CODE))
sys.path.insert(0, str(HERE.parent))

from adapters.qq import to_inbound  # noqa: E402

PASS = FAIL = 0


def check(name, ok, extra=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("PASS " + name)
    else:
        FAIL += 1
        print("FAIL " + name + (" | " + str(extra) if extra else ""))


def main():
    m = to_inbound(user_id="10001", text="在吗", images=["a.jpg"],
                   event_id="e1", reply_to="m1", store_text="入库")
    check("私聊 session", m.session == "private:10001", m.session)
    check("平台/外部 ID", m.platform == "qq" and m.external_user_id == "10001")
    check("图片", m.images == ["a.jpg"], m.images)
    check("事件/引用/入库", m.event_id == "e1" and m.reply_to == "m1"
          and m.store_text == "入库")

    g = to_inbound(user_id="10001", chat_id="20002", is_group=True, text="hi")
    check("群聊 session", g.session == "group:20002", g.session)
    check("群聊 chat_id", g.external_chat_id == "20002", g.external_chat_id)

    print(f"\n=== {PASS}/{PASS + FAIL} 通过 ===")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
