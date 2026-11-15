#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""export-persona.py —— 把当前使用的人格导成 Markdown/JSON 快照。

人格的权威来源是 /opt/nonebot/data/persona.json；本脚本只做「可读快照」，
写到 workspace/persona/，随每日备份带走。改人格请改 persona.json，别改快照。

用法：/opt/nonebot/venv/bin/python ops/export-persona.py（cron 每天 03:50）
"""
import json
import sys
from datetime import datetime
from pathlib import Path

SRC = Path("/opt/nonebot/data/persona.json")
OUT_DIR = Path("/opt/qqbot-data/workspace/persona")
LOG = Path("/opt/nonebot/ops/persona-export.log")


def log(msg: str) -> None:
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}\n"
    try:
        with LOG.open("a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass
    print(line, end="")


def load():
    if not SRC.exists():
        return []
    d = json.loads(SRC.read_text(encoding="utf-8"))
    return [{
        "persona_id": "editor-assistant",
        "system_prompt": d.get("system_prompt", ""),
        "begin_dialogs": d.get("begin_dialogs", []),
        "db_updated_at": datetime.fromtimestamp(SRC.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
        "exported_at": datetime.now().isoformat(timespec="seconds"),
    }]


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    items = load()
    if not items:
        log("no persona found（%s 不存在）" % SRC)
        return 1
    md = [f"# 人格导出（自动生成，勿手改）\n\n",
          f"- 来源：`{SRC}`\n",
          f"- 导出时间：{datetime.now():%Y-%m-%d %H:%M:%S}\n",
          "- 说明：改人格请改 `/opt/nonebot/data/persona.json`，本文件每天刷新。\n"]
    for it in items:
        md.append(f"\n---\n\n## {it['persona_id']}\n")
        md.append(f"- 更新时间：{it['db_updated_at']}\n")
        md.append(f"- system_prompt 长度：{len(it['system_prompt'])} 字\n")
        md.append(f"\n### system_prompt\n\n```text\n{it['system_prompt']}\n```\n")
        if it["begin_dialogs"]:
            md.append("\n### begin_dialogs（few-shot）\n\n```json\n"
                      + json.dumps(it["begin_dialogs"], ensure_ascii=False, indent=1) + "\n```\n")
    (OUT_DIR / "persona.md").write_text("".join(md), encoding="utf-8")
    (OUT_DIR / "persona.json").write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"ok: {len(items)} persona(s) -> {OUT_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
