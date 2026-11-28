#!/usr/bin/env python3
"""本地、CI 与云端共用的验证入口。"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

OPS = Path(__file__).resolve().parent
REPO_CODE = OPS.parent / "nonebot"
DEFAULT_CODE = REPO_CODE if (REPO_CODE / "app_core").is_dir() else OPS.parent

SUITES = {
    "lint-plugins.py": "quick pre all static".split(),
    "lint-layers.py": "quick pre all static".split(),
    "check-config-sync.py": "quick pre all ci".split(),
    "test-nonebot-offline.py": "pre all ci".split(),
    "test-package.py": "pre all ci".split(),
    "test-delivery-ledger.py": "pre all ci".split(),
    "test-followup-control.py": "pre all ci".split(),
    "test-turn-intent.py": "pre all ci".split(),
    "test-response-flow.py": "pre all ci".split(),
    "test-grounding.py": "pre all ci".split(),
    "test-provider-routes.py": "pre all ci".split(),
    "test-dates.py": "pre all ci".split(),
    "test-restore-drill.py": "pre all".split(),
    "ab-review.py": "pre all ci".split(),
    "test-harness.py": "quick pre all".split(),
    "test-mcp.py": "quick pre all".split(),
    "link-check.py": "quick pre all ci".split(),
    "smoke-all.py": "all ci".split(),
    "smoke-plugins.py": "all ci".split(),
    "test-lint-layers.py": "static".split(),
    "scan-secrets.py": "static".split(),
    "check-links.py": "static".split(),
    "test-check-links.py": "static".split(),
    "test-embed-guard.py": "static".split(),
    "test-kernel.py": "ci".split(),
    "test-engine.py": "ci".split(),
    "test-console.py": "ci".split(),
    "test-console-e2e.py": "ci".split(),
    "test-identity.py": "ci".split(),
    "test-delivery.py": "ci".split(),
    "test-approval.py": "ci".split(),
    "test-inbound.py": "ci".split(),
    "test-memory-port.py": "ci".split(),
    "test-batch4-guards.py": "ci".split(),
    "test-guards.py": "ci".split(),
    "test-runner.py": "ci".split(),
    "smoke-clock.py": "ci".split(),
    "test-batch2-guards.py": "ci".split(),
    "test-batch3-guards.py": "ci".split(),
    "test-deploy.py": "ci".split(),
}

GOLDEN = (
    ("test-prompt-golden.py",),
    ("test-prompt-golden.py", "--offline"),
    ("test-prompt-golden.py", "--selftest"),
)


def commands(profile: str) -> list[tuple[str, ...]]:
    selected = [(script,) for script, modes in SUITES.items() if profile in modes]
    if profile == "ci":
        selected.extend(GOLDEN)
    return selected


def run_suite(args: tuple[str, ...], env: dict[str, str], verbose: bool) -> bool:
    label = " ".join(args)
    path = OPS / args[0]
    if not path.is_file():
        print(f"FAIL {label} 找不到脚本", flush=True)
        return False
    start = time.monotonic()
    result = subprocess.run(
        [sys.executable, str(path), *args[1:]], env=env,
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    print(f"{'PASS' if result.returncode == 0 else 'FAIL'} {label} {time.monotonic() - start:.1f}s",
          flush=True)
    if result.returncode:
        lines = (result.stdout + result.stderr).splitlines()
        key = [line for line in lines if line.startswith(("FAIL", "失败项", "  - "))]
        print("\n".join(lines if verbose else key[:12] or lines[-15:]), flush=True)
    return result.returncode == 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=("quick", "pre", "all", "static", "ci"), default="all")
    parser.add_argument("--code", type=Path, default=Path(os.getenv("QQBOT_CODE_DIR", str(DEFAULT_CODE))))
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--list", action="store_true")
    options = parser.parse_args()
    selected = commands(options.profile)
    if options.list:
        print("\n".join(" ".join(args) for args in selected))
        return 0
    code = options.code.resolve()
    if not (code / "app_core").is_dir():
        parser.error(f"代码目录无效：{code}")
    env = dict(os.environ, QQBOT_CODE_DIR=str(code), PYTHONIOENCODING="utf-8")
    env.setdefault("DRIVER", "~websockets")
    passed = 0
    with tempfile.TemporaryDirectory(prefix="companion-verify-", ignore_cleanup_errors=True) as temp:
        for index, args in enumerate(selected):
            run_env = dict(env, QQBOT_ROOT=str(Path(temp) / str(index)))
            passed += run_suite(args, run_env, options.verbose)
    print(f"=== {passed}/{len(selected)} 通过 ===")
    return int(passed != len(selected))


if __name__ == "__main__":
    raise SystemExit(main())
