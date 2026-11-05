"""话题巡检：盯着几件会变的事，每周查一次，只报「真的变了」的。

和 web_search 的区别：那是**被动**的（他问才搜），这是**主动**的（不问也看）。
一个陪伴型的东西如果只会等着被问，永远跟不上时事——它得自己盯着。

设计上的三条：
1. **只报变化**。没变化就一个字不说——每周一条「没变化」会变成噪音，很快被无视。
2. **存储要留旧值**。没有 baseline 就判断不了「变没变」，所以每条话题存 `last`。
3. **失败静默**。某次搜不到就跳过，绝不把错误发给他。

话题存在 `/opt/nonebot/data/watchlist.json`，可以直接手改，不用重启（每次读盘）。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from app_core import paths as _paths

WATCH = _paths.DATA / "watchlist.json"
MAX_TOPICS = 8

COMPARE_SYSTEM = (
    "你在跟踪一个话题的最新情况。给你「上次记录」和「这次搜到的内容」，"
    "只判断有没有**实质变化**（价格、版本号、状态、新发布、政策口径）。\n"
    "有变化：用一句话说清变了什么，20-40 字，带具体数字或日期，不要复述背景。\n"
    "没变化、或者这次的信息还不足以判断：只输出一个「无」字。"
)


def load() -> list[dict]:
    """读话题表。格式：[{"topic": "...", "last": "...", "ts": 0}]"""
    try:
        data = json.loads(WATCH.read_text(encoding="utf-8"))
    except Exception:
        return []
    out = []
    for it in data if isinstance(data, list) else []:
        if isinstance(it, str):
            out.append({"topic": it.strip(), "last": "", "ts": 0})
        elif isinstance(it, dict) and str(it.get("topic") or "").strip():
            out.append({"topic": str(it["topic"]).strip(),
                        "last": str(it.get("last") or ""),
                        "ts": float(it.get("ts") or 0)})
    return out[:MAX_TOPICS]


def save(items: list[dict]) -> None:
    try:
        WATCH.parent.mkdir(parents=True, exist_ok=True)
        tmp = WATCH.with_suffix(".tmp")
        tmp.write_text(json.dumps(items[:MAX_TOPICS], ensure_ascii=False, indent=1),
                       encoding="utf-8")
        tmp.replace(WATCH)
    except Exception:
        pass


def seed(topics: list[str]) -> int:
    """第一次跑时用配置里的初始话题建表。已有文件就什么都不做。"""
    if WATCH.exists() or not topics:
        return 0
    save([{"topic": str(t).strip(), "last": "", "ts": 0}
          for t in topics if str(t).strip()])
    return len(load())


async def check_one(item: dict, search, complete) -> str:
    """查一个话题。返回变化描述；没变化/查不到返回空串。"""
    topic = item.get("topic") or ""
    if not topic:
        return ""
    try:
        found = await search(topic, 5)
    except Exception:
        return ""
    if not found or found.startswith(("没搜到", "搜索失败")):
        return ""
    last = item.get("last") or "（没有，这是第一次查：只说现在是什么状态，不要比较）"
    prompt = f"话题：{topic}\n\n上次记录：{last}\n\n这次搜到的：\n{found[:1800]}"
    try:
        raw = await complete([{"role": "system", "content": COMPARE_SYSTEM},
                              {"role": "user", "content": prompt}], 200)
    except Exception:
        return ""
    text = (raw or "").strip()
    if not text or text.startswith("无") or len(text) < 6:
        return ""
    item["last"] = text[:200]
    item["ts"] = time.time()
    return text


async def check_all(search, complete) -> list[str]:
    """跑一遍所有话题，返回变化列表（没变化的不会出现在里面）。"""
    items = load()
    if not items:
        return []
    changes: list[str] = []
    for it in items:
        line = await check_one(it, search, complete)
        if line:
            changes.append(f"{it['topic']}：{line}")
    save(items)
    return changes
