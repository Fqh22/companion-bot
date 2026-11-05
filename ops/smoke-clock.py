#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真实时钟冒烟：不 mock 时间，跑一遍时间敏感的链路。

为什么需要：离线回归里时间几乎都被 mock 掉了。进程重启、时钟跳变、定时任务晚跑，
这些真实条件下的行为测试覆盖不到——例如「20 小时没说话，情绪该衰减到哪」。

这个脚本**不断言具体数值**（那会随参数调整而碎），只断言三件事：不崩、值在范围内、
方向正确（衰减是单调的、过期真的会掉、整分真的会命中）。

用法：
    QQBOT_CODE_DIR=./nonebot python ops/smoke-clock.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

CODE = Path(os.environ.get("QQBOT_CODE_DIR", "/opt/nonebot")).resolve()
sys.path.insert(0, str(CODE))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

from app_core import mood, profile, scheduler  # noqa: E402

FAIL = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global FAIL
    print(("PASS " if cond else "FAIL ") + name + (f" | {extra}" if extra else ""))
    if not cond:
        FAIL += 1


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="clock-smoke-"))
    mood.STATE_DIR, mood.STATE = tmp, tmp / "state.json"
    now = time.time()

    st = dict(mood.load())
    st.update({"valence": -0.6, "valence_base": 0.0, "energy": 0.1,
               "social": 0.2, "updated_at": now - 6 * 3600})
    mood.save(st)
    a = mood.settle(mood.load())
    check("settle 不崩、三个值都在范围内",
          -1 <= a["valence"] <= 1 and 0 <= a["energy"] <= 1 and 0 <= a["social"] <= 1,
          str({k: round(a[k], 2) for k in ("valence", "energy", "social")}))
    check("心情朝基线的方向衰减",
          abs(a["valence"] - a["valence_base"]) <= abs(-0.6 - 0.0) + 1e-9,
          f"{a['valence']:.3f}")
    b = mood.settle(dict(a))
    check("同一时刻再 settle 一次几乎不变（幂等）",
          abs(b["valence"] - a["valence"]) < 1e-3,
          f"{a['valence']:.4f} -> {b['valence']:.4f}")

    res = profile.update_state({"家庭": "催婚"}, {"家庭": 0.9},
                               {"家庭": now - 121 * 86400}, {},
                               {}, {}, {}, {}, now)
    check("真实时钟下 121 天没证据 → 字段过期",
          "家庭" not in res["fields"], str(res["fields"]))
    res2 = profile.update_state({"家庭": "催婚"}, {"家庭": 0.9},
                                {"家庭": now - 10 * 86400}, {},
                                {}, {}, {}, {}, now)
    check("10 天没证据 → 字段还在", "家庭" in res2["fields"], str(res2["fields"]))

    d = datetime.now()
    ok, day, late = scheduler.Scheduler._due(d, d.hour, d.minute, 0)
    check("当前整分能命中调度", ok, f"{d.hour}:{d.minute} late={late} day={day}")
    far = (d.hour + 3) % 24
    ok2, _, _ = scheduler.Scheduler._due(d, far, d.minute, 0)
    check("非目标时刻不命中", not ok2, f"target={far}:{d.minute}")

    print(f"\n=== 真实时钟冒烟：{'全部通过' if not FAIL else f'{FAIL} 项失败'} ===")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
