"""对话规则：待办只在聊到相关词时注入（防翻旧账）。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

CODE = Path(os.environ.get("QQBOT_CODE_DIR", "/opt/nonebot")).resolve()
sys.path.insert(0, str(CODE))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

from app_core.chat_turn import _topic_overlap  # noqa: E402
from app_core.intent import looks_analytical  # noqa: E402
from app_core import relation_notes  # noqa: E402

RESULTS: list[bool] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    RESULTS.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + name + (f" | {extra}" if extra else ""))


def main() -> None:
    check("相关词触发", _topic_overlap("填 base_url 和 key", "base_url 我填了"))
    check("无关短句不触发", not _topic_overlap("填 base_url 和 key", "算了先这样"))
    check("同主题触发", _topic_overlap("交房租", "房东催房租了"))
    check("空串不触发", not _topic_overlap("", "随便问问"))
    check("单字不误触发", not _topic_overlap("买猫粮", "好"))

    # 分析类：疑问形式 + 分析词，两条都中
    check("方案靠谱吗触发", looks_analytical("这方案靠不靠谱"))
    check("sql 区别触发", looks_analytical("sql server和mysql有什么区别吗"))
    check("现在什么情况触发", looks_analytical("现在什么情况"))
    check("陈述不触发", not looks_analytical("我真想找个工作"))
    check("语气词不触发", not looks_analytical("还在学增删改查呢"))

    from app_core import situation
    check("空库时此刻为空", situation.describe("no-such-session") == "")
    check("空库时没有话题", situation.recent_topics("no-such-session") == [])

    relation_notes.path().unlink(missing_ok=True)
    check("没有小抄时为空", relation_notes.load() == "")
    relation_notes.path().parent.mkdir(parents=True, exist_ok=True)
    relation_notes.path().write_text("- 称呼：他叫我「你」", encoding="utf-8")
    check("有小抄时能读", "称呼" in relation_notes.load())

    passed = sum(1 for x in RESULTS if x)
    print(f"\n=== {passed}/{len(RESULTS)} 通过 ===")
    sys.exit(0 if passed == len(RESULTS) else 1)


if __name__ == "__main__":
    main()
