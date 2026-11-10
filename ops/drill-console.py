"""断线演练：假设 QQ 通道已经消失，只用 app_core + console 启动并对话。

不加载任何 NoneBot 插件、不连 OneBot。验证「没有 QQ 时这套东西还能不能活」，
并列出插件层提供的、脱离后会缺的能力。

用法（云端，有 keys）：
    printf '今天累死了\n这个报错又出来了\n/quit\n' | python ops/drill-console.py
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

SRC = Path(os.environ.get("QQBOT_SRC", "/opt"))
CODE = Path(os.environ.get("QQBOT_CODE_DIR", str(SRC / "nonebot"))).resolve()

# 临时根：不碰生产库/记忆；只借 keys、config、人格和 learn 里的画像/纠正。
TMP = Path(tempfile.mkdtemp(prefix="drill-"))
for rel in ("nonebot/config.yaml", "nonebot/data/persona.json",
            "qqbot-data/keys.json"):
    s = SRC / rel
    if s.exists():
        d = TMP / rel
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(s, d)
learn_src = SRC / "qqbot-data/workspace/learn"
learn_dst = TMP / "qqbot-data/workspace/learn"
if learn_src.exists():
    shutil.copytree(learn_src, learn_dst, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("*.bak*", "raw", "skills"))

os.environ["QQBOT_ROOT"] = str(TMP)
sys.path.insert(0, str(CODE))

from adapters.console import ConsoleChannel  # noqa: E402
from app_core.chat import ChatCore  # noqa: E402
from app_core.engine import Engine  # noqa: E402


async def main() -> None:
    core = ChatCore()
    await core.init_db()
    print(f"[drill] 根目录 {TMP}；没有 QQ、没有 NoneBot 插件")
    print(f"[drill] 人格 {len((core.persona or {}).get('system_prompt') or '')} 字；"
          f"记忆库 {TMP / 'qqbot-data/livingmemory.db'}")
    await ConsoleChannel().run(Engine(core))
    print("\n[drill] 结束")


if __name__ == "__main__":
    asyncio.run(main())
