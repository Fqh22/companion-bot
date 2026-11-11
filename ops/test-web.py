"""搜索结果摘要清理：HTML / 标题重复 / 站点模板。"""
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

from app_core.web import clean_summary  # noqa: E402

RESULTS: list[bool] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    RESULTS.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + name + (f" | {extra}" if extra else ""))


def main() -> None:
    title = "AI人才市場發生哪些變化？報告：從做AI走向用AI"
    raw = "# AI人才市場發生哪些變化？報告：從做AI走向用AI 2026年09月28日13:59 | 來源：人民網222 ..."
    out = clean_summary(raw, title)
    check("去掉标题符", not out.startswith("#"), out[:30])
    check("去掉站点模板", "來源" not in out and "来源" not in out, out[:40])
    check("不重复标题开头", not out.startswith(title[:6]), out[:30])

    out2 = clean_summary("<p>正文  内容</p>&nbsp;来源：某网 责任编辑：张三")
    check("去 HTML 和实体", "<" not in out2 and "&nbsp;" not in out2, out2)
    check("去责任编辑", "责任编辑" not in out2 and "来源" not in out2, out2)

    out3 = clean_summary("2026年09月28日13:59 | 人工智能正在创造新岗位")
    check("去开头时间戳", out3.startswith("人工智能"), out3[:20])

    long = "字" * 400
    check("截断到 240", len(clean_summary(long)) == 240, str(len(clean_summary(long))))

    passed = sum(1 for x in RESULTS if x)
    print(f"\n=== {passed}/{len(RESULTS)} 通过 ===")
    sys.exit(0 if passed == len(RESULTS) else 1)


if __name__ == "__main__":
    main()
