"""A/B：分析类问题，直接答 vs 先搜再答。

临时根 + 复制生产库，不碰生产；只跑 3 组，输出对比。
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

SRC = Path("/opt")
TMP = Path(tempfile.mkdtemp(prefix="ab-search-"))
(TMP / "nonebot").mkdir()
shutil.copytree(SRC / "nonebot/data", TMP / "nonebot/data",
                ignore=shutil.ignore_patterns("files"))
shutil.copytree(SRC / "qqbot-data", TMP / "qqbot-data",
                ignore=shutil.ignore_patterns("meme_packs"))
shutil.copy2(SRC / "nonebot/config.yaml", TMP / "nonebot/config.yaml")
os.environ["QQBOT_ROOT"] = str(TMP)
sys.path.insert(0, str(SRC / "nonebot"))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

from app_core import web  # noqa: E402
from app_core.chat import ChatCore  # noqa: E402

QUESTIONS = [
    "记忆契约的缺口在哪",
    "我该不该继续投简历",
    "AI 岗位现在什么形势",
]


async def main() -> None:
    core = ChatCore()
    for i, q in enumerate(QUESTIONS, 1):
        print(f"\n########## Q{i}: {q} ##########")
        chunks, _ = await core.reply(f"ab-test:{i}", q)
        print("\n=== A（直接答）===")
        print("".join(chunks)[:600])

        try:
            res = await web.search(q, 3)
        except Exception as e:  # noqa: BLE001
            res = f"搜索失败：{e}"
        prompt = f"{q}\n\n【我查到的资料，可作为依据；没用的可以不理】\n{res[:1500]}"
        chunks2, _ = await core.reply(f"ab-test:{i}", prompt)
        print("\n=== B（先搜再答）===")
        print("".join(chunks2)[:600])
    shutil.rmtree(TMP, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
