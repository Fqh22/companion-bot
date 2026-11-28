"""验证发布的门禁与切换顺序，使用本地桩替代 SSH。"""
from types import SimpleNamespace
from unittest.mock import patch
import subprocess

from deploy import publish
from harness import check, report


def run_case(*, no_restart=False, fail_pre=False):
    calls = []

    def run(args, **kwargs):
        if args[0] == "ssh":
            command = args[-1]
            calls.append((command, kwargs.get("input")))
            if fail_pre and "--pre" in command:
                raise subprocess.CalledProcessError(1, args)
        return subprocess.CompletedProcess(args, 0, stdout=b"old-release\n")

    def git(*args):
        if args[0] == "rev-parse":
            return b"abcd123\n"
        return b"archive" if args[0] == "archive" else b""

    options = SimpleNamespace(host="test-host", key="test-identity", no_restart=no_restart,
                              skip_verify=False, no_post_verify=False)
    with patch("deploy.git", git), patch("deploy.subprocess.run", run):
        try:
            publish(options)
        except subprocess.CalledProcessError:
            if not fail_pre:
                raise
    return calls


def main():
    calls = run_case()
    steps = [cmd for cmd, _ in calls]
    pre = next(i for i, cmd in enumerate(steps) if "--pre" in cmd)
    activate = next(i for i, cmd in enumerate(steps) if " activate " in cmd)
    health = next(i for i, cmd in enumerate(steps) if "systemctl is-active" in cmd)
    check("预检通过后切换，健康检查后跑全量", pre < activate < health < len(steps) - 1)
    check("代码和运维包通过二进制标准输入送达", [b for _, b in calls if b] == [b"archive"] * 2)
    check("预检失败时不切生产", not any(" activate " in c for c, _ in run_case(fail_pre=True)))
    check("候选模式不切生产", not any(" activate " in c for c, _ in run_case(no_restart=True)))
    return report()


if __name__ == "__main__":
    raise SystemExit(main())
