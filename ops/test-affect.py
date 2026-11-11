"""情绪契约（AffectPort）：读与旧路径一致、写能往返、状态量齐全。"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

CODE = Path(os.environ.get("QQBOT_CODE_DIR", "/opt/nonebot")).resolve()
sys.path.insert(0, str(CODE))
os.environ.setdefault("QQBOT_ROOT", tempfile.mkdtemp(prefix="affect-test-"))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

from app_core import mood, paths  # noqa: E402
from app_core.affect_port import CoreAffectPort  # noqa: E402

RESULTS: list[bool] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    RESULTS.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + name + (f" | {extra}" if extra else ""))


def main() -> None:
    paths.DATA.mkdir(parents=True, exist_ok=True)
    ap = CoreAffectPort()

    s = ap.read()
    check("read 含四个量", set(s) >= {"valence", "valence_base", "energy", "social"},
          str(sorted(s))[:60])
    ref = mood.settle(mood.load())
    same = all(abs(float(s.get(k, 0)) - float(ref.get(k, 0))) < 1e-9
               for k in ("valence", "valence_base", "energy", "social"))
    check("read 与旧路径一致", same, str({k: (s.get(k), ref.get(k)) for k in ("valence", "energy")}))

    s2 = dict(s)
    s2["energy"] = 0.42
    ap.write(s2)
    check("write 后能读回", abs(float(mood.load().get("energy", 0)) - 0.42) < 1e-6,
          str(mood.load().get("energy")))

    s3 = dict(s2)
    out = ap.observe(s3, "今天真累", vec=None)
    check("observe 返回新状态", isinstance(out, dict) and "valence" in out)
    check("observe 后状态可读", bool(out) and "energy" in out)

    passed = sum(1 for x in RESULTS if x)
    print(f"\n=== {passed}/{len(RESULTS)} 通过 ===")
    sys.exit(0 if passed == len(RESULTS) else 1)


if __name__ == "__main__":
    main()
