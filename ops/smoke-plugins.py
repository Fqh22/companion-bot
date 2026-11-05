"""插件启动体检：真的把 5 个插件加载一遍。

离线回归只测 app_core；插件（消息接入、命令、调度、主动消息）一行没跑过。
插件导入失败 = 整个机器人起不来，所以这层必须单独验。

用法：
    QQBOT_CODE_DIR=<仓库>/nonebot python ops/smoke-plugins.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

CODE = Path(os.environ.get("QQBOT_CODE_DIR", "/opt/nonebot")).resolve()
sys.path.insert(0, str(CODE))
os.chdir(tempfile.mkdtemp(prefix="plug-"))     # 避免读到仓库里的 .env

import nonebot  # noqa: E402
from nonebot.adapters.onebot.v11 import Adapter  # noqa: E402

nonebot.init()
nonebot.get_driver().register_adapter(Adapter)
# load_plugins 要求插件目录在 cwd 下面（和 bot.py 一样），所以切到代码目录再加载
os.chdir(CODE)
loaded = nonebot.load_plugins("plugins")

print("加载到的插件：")
for p in loaded:
    print("  -", p.name if hasattr(p, "name") else p)

try:
    from nonebot.matcher import matchers       # NoneBot 2.5 的临时/全局注册表
    n_matchers = len(matchers)
except Exception:                              # 版本差异时退回 0，不作为硬指标
    n_matchers = 0

# 每个插件该暴露的关键对象：缺了说明导入期就出了问题
EXPECT = {
    "plugins.chat": ["core", "driver", "_handle", "_init", "_catch_up"],
    "plugins.group": ["core", "driver", "_handle", "_init"],
    "plugins.commands": ["mood_cmd", "mood_hist", "kb_verify", "kb_deprecate", "raw_cmd", "restore_cmd"],
    "plugins.admin": ["ping", "help_cmd", "reload_cmd"],
    "plugins.schedule": ["sched", "_send"],
    "plugins.proactive": ["_tick", "_compose"],
    "plugins.agent": ["_task_cmd", "_poll_once", "_loop", "_control"],
}
EXPECT_COUNT = len(EXPECT)      # 加插件时只改 EXPECT，不用再找魔数
missing = []
for mod, names in EXPECT.items():
    m = sys.modules.get(mod)
    if m is None:
        missing.append(mod + "(未导入)")
        continue
    missing += [f"{mod}.{n}" for n in names if not hasattr(m, n)]

print(f"注册的 matcher 数：{n_matchers}")
ok = len(loaded) == EXPECT_COUNT and not missing
print(("PASS " if ok else "FAIL ") +
      f"插件加载（{len(loaded)}/{EXPECT_COUNT} 插件，缺 {len(missing)} 个关键对象）")
if missing:
    print("  缺：", "、".join(missing))
sys.exit(0 if ok else 1)
