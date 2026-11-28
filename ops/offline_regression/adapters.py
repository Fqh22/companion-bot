"""可独立运行的离线回归用例。"""
from offline_regression.fixtures import (
    check,
)


async def run():
    # ---------- 飞书入站归一化（adapters/feishu.py，纯函数，不碰 lark） ----------
    from adapters.feishu import parse_content as _fp
    from adapters.feishu import to_inbound as _fi

    _p = _fp("text", '{"text": "在吗"}')
    check("飞书 text 解析", _p["text"] == "在吗", str(_p))
    _p = _fp("post", '{"title": "标题", "content": [[{"tag": "text", "text": "正文"},'
                     '{"tag": "img", "image_key": "img_x"}]]}')
    check("飞书 post 解析（标题+正文+内嵌图）",
          "标题" in _p["text"] and "正文" in _p["text"]
          and _p["image_keys"] == ["img_x"], str(_p))
    _p = _fp("image", '{"image_key": "img_y"}')
    check("飞书 image 解析", _p["image_keys"] == ["img_y"], str(_p))
    _p = _fp("audio", '{"file_key": "file_x", "duration": 1000}')
    check("飞书 audio 解析", _p["audio_key"] == "file_x", str(_p))
    _p = _fp("text", '{"text": "@_user_1 你好"}')
    check("飞书 @ 占位被清掉", _p["text"] == "你好", _p["text"])
    _m = _fi(open_id="ou_x", chat_id="oc_y", text="hi",
             images=["/tmp/a.jpg"], event_id="m1", voice=True, session="private:1")
    check("飞书 to_inbound 映射",
          _m.platform == "feishu" and _m.session == "private:1"
          and _m.voice and _m.images == ["/tmp/a.jpg"], str(_m))

    # ---------- 群聊：限流 / 上下文裁剪 / 隔离 ----------
    from app_core import group as group_mod

    now_g = 1_000_000.0
    check("冷却期内不触发", not group_mod.rate_ok([now_g - 2], now_g, 3, 5))
    check("过了冷却且没超频可以触发", group_mod.rate_ok([now_g - 10], now_g, 3, 5))
    check("一分钟内到上限就停",
          not group_mod.rate_ok([now_g - 10, now_g - 8, now_g - 6], now_g, 3, 1),
          "3 条/分钟")
    check("窗口外的旧记录不再计数",
          group_mod.rate_ok([now_g - 120, now_g - 110], now_g, 3, 5))

    many = [("user", f"第{i}句") for i in range(30)]
    kept = group_mod.trim_context(many, 20, 10 ** 6)
    check("上下文按条数裁剪", len(kept) == 20 and kept[-1][1] == "第29句",
          f"{len(kept)} {kept[-1] if kept else None}")
    kept_c = group_mod.trim_context([("user", "字" * 600)] * 10, 20, 1500)
    total_c = sum(len(t) for _, t in kept_c)
    check("上下文按字数裁剪", total_c <= 1500 and len(kept_c) >= 2,
          f"{len(kept_c)} 条 / {total_c} 字")

    gc = group_mod.GroupCore()
    check("群聊默认关闭", gc.enabled is False)
    check("白名单为空（空跑）", not gc.whitelist, str(gc.whitelist))
    for i in range(25):
        gc.remember("g1", "user", f"第{i}句")
    check("内存上下文不超过 20 条", len(gc.context("g1")) <= 20, str(len(gc.context("g1"))))
    check("不同群上下文隔离", not gc.context("g2"))
    check("群聊没有记忆表依赖", "memory" not in dir(group_mod), "")
