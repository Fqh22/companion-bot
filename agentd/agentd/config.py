"""配置：路径、模型、预算、沙箱。全部可用环境变量覆盖，方便测试。"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(os.environ.get("AGENTD_ROOT", "/opt/agentd"))
DATA = Path(os.environ.get("AGENTD_DATA", str(ROOT / "data")))
WORK = Path(os.environ.get("AGENTD_WORK", str(ROOT / "work")))
LOGS = WORK / ".logs"
KEYS_FILE = Path(os.environ.get("AGENTD_KEYS", "/opt/qqbot-data/keys.json"))


def _keys() -> dict:
    try:
        return json.loads(KEYS_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


@dataclass
class Config:
    api: str = "https://api.deepseek.com/v1/chat/completions"
    search_api: str = "https://api.deepseek.com/v1/tools/search"
    model: str = "deepseek-flash"
    summary_model: str = "deepseek-chat"
    api_key: str = ""
    # 预算（任一触顶即停）
    max_steps: int = 30
    max_tokens: int = 150_000
    max_seconds: int = 1200
    daily_tokens: int = 500_000
    # 上下文
    max_context_tokens: int = 12_000
    digest_max_chars: int = 300
    compact_every: int = 5
    # 沙箱
    sandbox: str = "agentd-sandbox"
    net: str = "agentd-net"
    proxy_host: str = "agentd-proxy"
    proxy_port: int = 8888
    # 768m：512m 下 pip 装 lxml/pandas 会被 OOM 杀掉（实测 exit 137）
    sandbox_memory: str = "768m"
    exec_timeout: int = 30
    out_inline: int = 4096
    idle_stop_seconds: int = 600
    # 通道
    port: int = 8100
    # 默认只回结果：任务 20 秒内结束就一句话都不多说；
    # 超过 20 秒才发一条「还在跑」，超过 3 分钟再补一条。全过程最多 2 条。
    ack_after_seconds: int = 20
    progress_after_seconds: int = 180
    milestone_cap: int = 5
    heartbeat_cap: int = 2
    # 记忆
    rag_api: str = "http://127.0.0.1:8765"
    rag_project: str = "agentd"
    # 记忆库探测结果：不可用时把两个记忆工具从工具表里摘掉，别为死工具付 token
    memory_enabled: bool = True
    # 任务结果写进这里 → 陪伴机器人的每小时索引会自动收录（单向同步）。
    # 目录不存在就不写，方便在没有机器人的机器上单跑。
    handoff_note_dir: str = "/opt/qqbot-data/workspace/learn/notes"
    extra: dict = field(default_factory=dict)

    @classmethod
    def load(cls) -> "Config":
        c = cls()
        k = _keys()
        c.api_key = k.get("deepseek", "")
        f = ROOT / "config.json"
        if f.exists():
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                d = {}
            for kk, vv in d.items():
                if hasattr(c, kk):
                    setattr(c, kk, vv)
        for e, attr in (("AGENTD_MODEL", "model"), ("AGENTD_PORT", "port"),
                        ("AGENTD_MAX_STEPS", "max_steps")):
            if os.environ.get(e):
                val = os.environ[e]
                setattr(c, attr, int(val) if val.isdigit() else val)
        return c


def ensure_dirs() -> None:
    for p in (DATA, WORK, LOGS):
        p.mkdir(parents=True, exist_ok=True)
