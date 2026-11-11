"""Prompt A/B：同一批真实问题，比较「规则版」与「例子版」的回复。

用法：python ops/ab-prompt.py a|b
输出写到生产 data/ab-<tag>.txt（方便下载对比）。
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

TAG = sys.argv[1] if len(sys.argv) > 1 else "a"
SRC = Path("/opt")
TMP = Path(tempfile.mkdtemp(prefix=f"abp-{TAG}-"))
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

from app_core.chat import ChatCore  # noqa: E402

QUESTIONS = [
    "记忆契约的缺口在哪",
    "我该不该继续投简历",
    "AI 岗位现在什么形势",
]


async def main() -> None:
    core = ChatCore()
    lines = [f"##### tag={TAG} #####"]
    for i, q in enumerate(QUESTIONS, 1):
        chunks, _ = await core.reply(f"abp-{TAG}:{i}", q)
        text = "".join(chunks).strip()
        lines.append(f"\n########## Q{i}: {q} ##########\n{text}")
    (SRC / "nonebot/data" / f"ab-{TAG}.txt").write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    shutil.rmtree(TMP, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
