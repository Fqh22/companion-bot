"""[orchestration] 现实校验：回复里的时间词/现势断言，和当下对不对得上。

只记录，不改回复。影子跑一周，看误报率再决定要不要重写；
重写也必须保留原回复的意图和人格，不能从零生成。
"""
from __future__ import annotations

import json
import sqlite3
import time

from app_core import paths as _paths

LOG = _paths.DATA / "reality_check.jsonl"
MAX_BYTES = 512 * 1024

# 离线趋势用的词表。采样不是检测器：这些词会误伤（「积累」不是累、
# 「帮忙」不是忙），先排除，等真有语义层再换。
GROUPS = {
    "累": ("累", "撑不住", "快废了", "疲惫", "扛不住", "没力气"),
    "失眠": ("睡不着", "失眠", "没睡", "凌晨"),
    "熬夜": ("熬夜", "通宵"),
    "忙": ("忙", "加班", "没空"),
}
_NOT = ("积累", "累积", "累计", "不累", "累不累", "帮忙", "不忙",
        "赶紧", "赶快", "赶不上")

_MORNING = ("早上好", "早安", "睡醒了", "起床了", "早啊")
_NIGHT = ("晚上好", "晚安", "还没睡", "睡不着")
_LIVE = ("今天", "昨天", "现在", "最新", "价格", "多少钱", "天气",
         "新闻", "汇率", "股价", "气温")


def weekly(days: int = 7, session: str = "") -> list[dict]:
    """最近 N 天的现实痕迹：次数 + 日期。只陈述，不解释原因。

    给周报和 offline 采样共用；不是趋势检测器——LIKE 会漏会误，
    真做趋势要语义层。这里只回答「说了几次、哪几天」。
    """
    since = time.time() - max(1, int(days)) * 86400
    sql = ("SELECT content, ts FROM messages WHERE role='user' AND ts>=?")
    args: list = [since]
    if session:
        sql += " AND session=?"
        args.append(session)
    sql += " ORDER BY id"
    try:
        c = sqlite3.connect(f"file:{_paths.CHAT_DB}?mode=ro", uri=True, timeout=10)
        rows = c.execute(sql, args).fetchall()
        c.close()
    except Exception:  # noqa: BLE001
        return []
    out = {k: [] for k in GROUPS}
    for text, ts in rows:
        s = (text or "").strip()
        if not s or s.startswith("/") or s.startswith("[发了图片]"):
            continue
        if any(w in s for w in _NOT):
            continue
        t = time.strftime("%m-%d %H:%M", time.localtime(float(ts)))
        for name, words in GROUPS.items():
            if any(w in s for w in words):
                out[name].append(t)
                break
    return [{"name": k, "count": len(v), "times": v} for k, v in out.items() if v]


def check(text: str, hour: int, source: str = "", rid: str = "",
          session: str = "") -> list[str]:
    """返回命中的冲突类型；没有就返回空列表。只读文本 + 写日志。"""
    s = (text or "").strip()
    if not s:
        return []
    hits: list[str] = []
    if 5 <= hour < 11:
        if any(w in s for w in _NIGHT):
            hits.append("night_word_in_morning")
    elif hour >= 17 or hour < 5:
        if any(w in s for w in _MORNING):
            hits.append("morning_word_at_night")
    if source == "无" and any(w in s for w in _LIVE):
        hits.append("live_claim_no_source")
    if not hits:
        return []
    try:
        _paths.DATA.mkdir(parents=True, exist_ok=True)
        try:
            if LOG.stat().st_size > MAX_BYTES:
                lines = LOG.read_text(encoding="utf-8", errors="replace").splitlines()
                LOG.write_text(chr(10).join(lines[-400:]) + chr(10), encoding="utf-8")
        except FileNotFoundError:
            pass
        with LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.time(), "kind": ",".join(hits),
                                "hour": hour, "source": source, "rid": rid,
                                "session": session, "text": s[:120]},
                               ensure_ascii=False) + chr(10))
    except Exception:  # noqa: BLE001
        pass
    return hits