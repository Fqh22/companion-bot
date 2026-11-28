"""离线回归共用夹具。生产库仅通过 SQLite 备份到临时目录。"""
import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

# Windows 控制台默认 GBK，打印 emoji / 生僻字会直接 UnicodeEncodeError 把测试打断。
# 放在这里是为了在任何输出之前生效；不支持 reconfigure 的环境静默跳过。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass
WORKTREE_CODE = Path(__file__).resolve().parents[2] / "nonebot"
IN_WORKTREE = (WORKTREE_CODE / "app_core").is_dir()
CODE_DIR = os.environ.get("QQBOT_CODE_DIR", str(WORKTREE_CODE) if IN_WORKTREE else "/opt/nonebot")
sys.path.insert(0, CODE_DIR)

TMP = Path(tempfile.mkdtemp(prefix="qqbot-test-"))
# paths.py 在 import 时固化 QQBOT_ROOT：必须赶在 import app_core 之前把根指到临时目录。
# 下面的逐个重定向只覆盖了 8 个模块，wiki / shadow / diary / meme / contradict / scheduler /
# watch 仍读默认根；云端默认根就是 /opt —— 那等于测试直接写生产库（2026-11-10 实测）。
DEFAULT_ROOT = Path(os.environ.get("QQBOT_ROOT") or "/opt")
os.environ["QQBOT_ROOT"] = str(TMP / "root")
print("代码目录:", CODE_DIR)
print("临时目录:", TMP)

# 工作树默认用空库夹具。部署目录保留生产库只读复制，可显式指定其他来源。
SRC_MEM = Path(os.environ.get("QQBOT_MEM_SRC", str(TMP / "source-memory.db") if IN_WORKTREE
                             else "/opt/qqbot-data/livingmemory.db"))
SRC_CHAT = Path(os.environ.get("QQBOT_CHAT_SRC", str(TMP / "source-chat.db") if IN_WORKTREE
                              else "/opt/nonebot/data/chat.db"))


def copy_db(src: Path, dst: Path) -> None:
    if not src.exists():
        dst.touch()
        return
    s = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    d = sqlite3.connect(dst)
    with d:
        s.backup(d)
    s.close()
    d.close()


copy_db(SRC_MEM, TMP / "livingmemory.db")
copy_db(SRC_CHAT, TMP / "chat.db")

from app_core import chat as chat_mod          # noqa: E402
from app_core import config as config_mod      # noqa: E402
from app_core import files as files_mod        # noqa: E402
from app_core import followup as followup_mod  # noqa: E402
from app_core import kb as kb_mod              # noqa: E402
from app_core import learn as learn_mod        # noqa: E402
from app_core import learn_store as learn_store_mod  # noqa: E402
from app_core import memory as memory_mod      # noqa: E402

from app_core import mood as mood_mod          # noqa: E402
from app_core import mood_store as mood_store_mod  # noqa: E402
from app_core import summary as summary_mod   # noqa: E402
from app_core import history as history_mod   # noqa: E402
from app_core import chat_reply as chat_reply_mod  # noqa: E402
from app_core import followup_store as followup_store_mod  # noqa: E402
from app_core import remind as remind_mod      # noqa: E402
from app_core.scheduling import remind as remind_store_mod  # noqa: E402
from app_core import summarize as summarize_mod  # noqa: E402
from app_core import tools as tools_module    # noqa: E402

# 下面多处会把 tools.execute 换成桩；跑完要还原，否则同进程里后面的检查
# （以及 smoke-all.py）看到的都是假实现，真实现的问题永远测不出来。
REAL_TOOLS_EXECUTE = chat_mod.tools.execute

# 所有落盘路径都改到临时目录，确保不碰生产
chat_mod.DB = TMP / "chat.db"
chat_mod.DATA = TMP           # 只在 init_db 里用来建目录，Windows 上跑测试也不会碰 /opt
summary_mod.DB = TMP / "chat.db"   # 摘要/水位已拆到 SummaryMixin（2026-11-16）
history_mod.DB = TMP / "chat.db"   # 历史读写已拆到 HistoryMixin（2026-11-16）
# chat_reply 的 DB 通过 _chat().DB 读，测试补 chat_mod.DB 即可（2026-11-16）
memory_mod.DB = TMP / "livingmemory.db"
kb_mod.DB = TMP / "chat.db"
mood_store_mod.STATE_DIR = TMP / "mood"
mood_store_mod.STATE = TMP / "mood" / "state.json"
mood_store_mod.CHAT_DB = TMP / "chat.db"
learn_mod.RAW = TMP / "raw"
learn_mod.CHAT_DB = TMP / "chat.db"
remind_store_mod.DB = TMP / "chat.db"
followup_store_mod.DB = TMP / "chat.db"
files_mod.SAVE_DIR = TMP
files_mod.IMAGE_DIR = TMP / "images"
files_mod.PENDING_JSON = TMP / "last-file.json"
# 后面的 reply 测试会把 kb.recall 换成桩，这里先留一份真的给知识库用例用
REAL_KB_RECALL = kb_mod.recall

RESULTS = []


def check(name: str, cond: bool, extra: str = "") -> None:
    RESULTS.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + name + ((" | " + extra) if extra else ""))


def check_root_isolated() -> None:
    """回归保护：确认 import 之前设的 QQBOT_ROOT 真的生效（有人挪了位置就会红）。"""
    import app_core.paths as _paths
    check("默认根已指向临时目录（不写 %s）" % DEFAULT_ROOT,
          str(_paths.ROOT) == str(TMP / "root"), str(_paths.ROOT))
