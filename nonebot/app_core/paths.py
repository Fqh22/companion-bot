"""运行时路径：默认 /opt，可用 QQBOT_ROOT 改根。

为什么要单独一层：代码里原来散落着 30 处 Path("/opt/...")，换安装目录要改十几个文件，
还容易漏一处——那份漏掉的会静默写回 /opt。

约定：
    QQBOT_ROOT=/opt                       # 默认
    $QQBOT_ROOT/nonebot/data              # chat.db、人格、影子日志、prompt 快照
    $QQBOT_ROOT/qqbot-data                # 记忆库、keys.json、学习资料
    $QQBOT_ROOT/agentd                    # agent 工作区与收件箱
    $QQBOT_ROOT/backup                    # 备份
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(os.environ.get("QQBOT_ROOT") or "/opt")
NB = ROOT / "nonebot"
DATA = NB / "data"
QDATA = ROOT / "qqbot-data"
AGENTD = ROOT / "agentd"
BACKUP = ROOT / "backup"

KEYS = QDATA / "keys.json"
LEARN = QDATA / "workspace" / "learn"
CHAT_DB = DATA / "chat.db"
MEM_DB = QDATA / "livingmemory.db"
