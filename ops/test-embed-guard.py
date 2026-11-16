#!/usr/bin/env python3
"""护栏自检：embedding provider 的默认值与读侧维度守卫（纯逻辑，无第三方依赖）。

对应 AGENTS.md「新行为配一条 check()」。这些规则如果写错，线上不会报错，
只会静默退化成 recent()——所以必须在离线用例里把反例钉死。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "nonebot"))
from app_core import embed_provider as ep  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("PASS " if ok else "FAIL ") + name + (f" | {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


def main() -> int:
    z = ep.spec("zhipu")
    s = ep.spec("siliconflow")
    check("zhipu 默认 2048 维", z["dim"] == 2048, str(z))
    check("siliconflow 默认 1024 维", s["dim"] == 1024, str(s))
    check("provider 给出 api/model", bool(z["api"]) and "embedding-3" == z["model"])
    check("显式 dim 覆盖 provider", ep.spec("zhipu", dim=1024)["dim"] == 1024)
    check("显式 api 覆盖 provider", ep.spec("zhipu", api="https://example/v1")["api"]
          == "https://example/v1")
    check("未知 provider 不猜维度", ep.spec("nope")["dim"] == 0)

    check("维度一致可继续", ep.check_dim(2048, 2048, 2048)[0] is True)
    mismatch = ep.check_dim(2048, 1024, 2048)
    check("矩阵/查询维度不符被拦", mismatch[0] is False and "重建" in mismatch[1],
          mismatch[1])
    cfg_bad = ep.check_dim(1024, 1024, 2048)
    check("查询/配置维度不符被拦", cfg_bad[0] is False, cfg_bad[1])
    check("未配置维度时不误报", ep.check_dim(1024, 1024, 0)[0] is True)
    check("空矩阵且查询与配置一致时不误报", ep.check_dim(0, 2048, 2048)[0] is True)

    total = 11 - len(FAILS)
    print(f"\n=== {total}/11 通过 ===" if not FAILS else
          f"\n=== FAIL: {', '.join(FAILS)} ===")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
