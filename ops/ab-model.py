"""模型 A/B：同一批真实问题，flash vs chat。

用法：python ops/ab-model.py flash|chat
临时根 + 复制生产库；只改 llm.model，其它不动。
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

MODEL = sys.argv[1] if len(sys.argv) > 1 else "flash"
SRC = Path("/opt")
TMP = Path(tempfile.mkdtemp(prefix=f"abm-{MODEL}-"))
(TMP / "nonebot").mkdir()
shutil.copytree(SRC / "nonebot/data", TMP / "nonebot/data",
                ignore=shutil.ignore_patterns("files"))
shutil.copytree(SRC / "qqbot-data", TMP / "qqbot-data",
                ignore=shutil.ignore_patterns("meme_packs"))
cfg = (SRC / "nonebot/config.yaml").read_text(encoding="utf-8")
cfg = re.sub(r"(?m)^(\s*)model:\s*deepseek-flash\s*$",
             r"\1model: deepseek-" + ("chat" if MODEL == "chat" else "flash"), cfg)
(TMP / "nonebot/config.yaml").write_text(cfg, encoding="utf-8")
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
    "我现在这个方向还有没有戏",
    "你觉得我该先补经验还是先投",
]


async def main() -> None:
    core = ChatCore()
    lines = [f"##### model={MODEL} #####"]
    for i, q in enumerate(QUESTIONS, 1):
        chunks, _ = await core.reply(f"abm-{MODEL}:{i}", q)
        text = "".join(chunks).strip()
        lines.append(f"\n########## Q{i}: {q} ##########\n{text}")
    (SRC / "nonebot/data" / f"ab-model-{MODEL}.txt").write_text(
        "\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    shutil.rmtree(TMP, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
