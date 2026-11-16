#!/usr/bin/env python3
"""[infra] 分层检查的反例自检：每条规则都必须能抓到故意写坏的文件。

为什么单独一个脚本：lint 规则最容易「看起来在跑，其实永远绿」——
2026-11 就出现过 check_plugins 只剩一个 pass，而文档写着「由 lint 强制」。
这里在临时目录造 6 类反例，逐条确认 lint-layers 会红，再确认合法集能绿。
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

HERE = Path(__file__).resolve().parent
LINT = HERE / "lint-layers.py"
PY = sys.executable
RESULTS: list[bool] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    RESULTS.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  {extra}"))


def write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def run(core: Path) -> tuple[int, str]:
    r = subprocess.run([PY, str(LINT), "--core", str(core)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    return r.returncode, r.stdout + r.stderr


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="lint-layers-"))
    core = tmp / "app_core"

    # 合法集：domain 依赖 domain，orchestration 依赖谁都行，infra 依赖 paths
    write(core, "vec.py", '"""[domain] 纯向量工具"""\n')
    write(core, "prompt.py", '"""[domain] 拼装"""\nfrom app_core import vec\n')
    write(core, "paths.py", '"""[infra] 路径"""\n')
    write(core, "chat.py", '"""[orchestration] 编排"""\nfrom app_core import prompt, paths\n')
    rc, out = run(core)
    check("合法集 PASS", rc == 0, out[-500:])

    # 反例 1：缺层标记
    write(core, "nomark.py", '"""没有层标记"""\n')
    rc, out = run(core)
    check("缺层标记 -> FAIL", rc == 1 and "nomark.py" in out and "缺层标记" in out, out[-400:])
    (core / "nomark.py").unlink()

    # 反例 2：domain import 编排层
    write(core, "bad_dep.py", '"""[domain] 坏依赖"""\nfrom app_core import chat\n')
    rc, out = run(core)
    check("domain -> orchestration -> FAIL", rc == 1 and "bad_dep.py" in out and "编排层" in out, out[-400:])
    (core / "bad_dep.py").unlink()

    # 反例 3：domain 里 import IO 库
    write(core, "bad_io.py", '"""[domain] 坏 IO"""\nimport sqlite3\n')
    rc, out = run(core)
    check("domain -> IO import -> FAIL", rc == 1 and "bad_io.py" in out and "IO import" in out, out[-400:])
    (core / "bad_io.py").unlink()

    # 反例 4：infra import 编排层
    write(core, "bad_infra.py", '"""[infra] 坏地基"""\nfrom app_core import chat\n')
    rc, out = run(core)
    check("infra -> orchestration -> FAIL", rc == 1 and "bad_infra.py" in out, out[-400:])
    (core / "bad_infra.py").unlink()

    # 反例 5：kernel import 业务包
    write(core, "kernel/db.py", '"""[infra] kernel"""\nfrom app_core import prompt\n')
    rc, out = run(core)
    check("kernel -> 业务包 -> FAIL", rc == 1 and "kernel" in out and "业务包" in out, out[-400:])
    (core / "kernel" / "db.py").unlink()

    # 反例 6：adapters 越过门面
    write(tmp, "adapters/bad.py", '"""adapter"""\nfrom app_core import chat\n')
    rc, out = run(core)
    check("adapters -> chat -> FAIL", rc == 1 and "adapters" in out, out[-400:])
    (tmp / "adapters" / "bad.py").unlink()

    # 清掉反例后必须回到 PASS
    rc, out = run(core)
    check("反例清掉后回到 PASS", rc == 0, out[-400:])

    passed = sum(1 for r in RESULTS if r)
    print(f"\n=== {passed}/{len(RESULTS)} 通过 ===")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
