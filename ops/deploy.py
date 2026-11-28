#!/usr/bin/env python3
"""Bash 与 PowerShell 共用的发布流程。"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from shlex import quote

ROOT = Path(__file__).resolve().parent.parent
CURRENT = "/opt/nonebot"
RELEASES = "/opt/nonebot-releases"


def git(*args: str) -> bytes:
    return subprocess.check_output(["git", *args], cwd=ROOT)


def publish(options) -> None:
    ssh = ["ssh", "-i", options.key, "-o", "ProxyCommand=none",
           "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=15",
           "-o", "ServerAliveInterval=20", options.host]

    def remote(command: str, *, capture: bool = False, data: bytes | None = None):
        return subprocess.run([*ssh, command], input=data, capture_output=capture, check=True)

    subprocess.run([sys.executable, str(ROOT / "ops/scan-secrets.py")], cwd=ROOT, check=True)
    sha = git("rev-parse", "--short", "HEAD").decode().strip()
    if not re.fullmatch("[0-9a-f]+", sha):
        raise ValueError("无法识别当前 Git 提交")
    if git("status", "--porcelain"):
        print(f"WARN 有未提交改动，发布内容来自 HEAD（{sha}）", flush=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    release_id = f"{sha}-{ts}"
    release = f"{RELEASES}/{release_id}"
    switch = f"{release}/ops/release-switch.sh"
    previous = remote(
        f'old=$(cat {CURRENT}/.release_id 2>/dev/null); '
        'if [ -n "$old" ]; then printf "%s" "$old"; '
        f'else old=$(cat {CURRENT}/.deployed_sha 2>/dev/null); '
        'printf "legacy-%s" "${old:-unknown}"; fi',
        capture=True).stdout.decode().strip()
    if not re.fullmatch(r"[0-9a-zA-Z._-]+", previous):
        previous = "legacy-unknown"
    rollback = f"sudo -n bash {switch} rollback {quote(previous)}"
    snapshot = f"/opt/backup/code-{ts}.tgz"

    print(f"01 保存旧版本快照 {snapshot}", flush=True)
    remote(f"sudo -n mkdir -p /opt/backup && sudo -n tar czf {snapshot} "
           f"-C {CURRENT} --ignore-failed-read app_core plugins adapters companion_core "
           "personas ops bot.py config.example.yaml requirements.txt README.md .env.example .deployed_sha")
    print(f"02 构建候选版本 {release}", flush=True)
    remote(f"sudo -n mkdir {release} && sudo -n mkdir {release}/ops")
    for tree, dest in (("nonebot", release), ("ops", f"{release}/ops")):
        remote(f"sudo -n tar -x -C {dest}", data=git("archive", f"HEAD:{tree}"))
    remote(f"sudo -n chmod +x {release}/ops/*.sh && sudo -n chown -R ubuntu:ubuntu {release}"
           f" && sudo -n bash {switch} prepare {release}")
    for marker, value in ((".deployed_sha", sha), (".release_id", release_id)):
        remote(f"printf '%s\\n' {value} | sudo -n tee {release}/{marker} >/dev/null")

    if not options.skip_verify:
        print("03 验证候选版本", flush=True)
        try:
            remote(f"sudo -n {release}/ops/verify.sh --pre --code {release}")
        except subprocess.CalledProcessError:
            print(f"候选验证失败，现场保留在 {release}", file=sys.stderr)
            raise
    if options.no_restart:
        print(f"候选已准备好，生产服务继续运行旧版本。{release}")
        return

    print(f"04 切换到 {release_id}", flush=True)
    try:
        remote(f"sudo -n bash {switch} activate {release} {release_id}")
        remote(f"cat {CURRENT}/.deployed_sha && systemctl is-active nonebot")
        if not options.no_post_verify:
            print("05 验证已发布版本", flush=True)
            remote(f"sudo -n {CURRENT}/ops/verify.sh")
    except subprocess.CalledProcessError:
        print(f"检查当前版本。云端回滚命令为 {rollback}", file=sys.stderr)
        raise
    print(f"已发布 {release_id}，云端回滚命令为 {rollback}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=os.getenv("QQBOT_HOST"))
    parser.add_argument("--key", default=os.getenv("QQBOT_KEY"))
    parser.add_argument("--no-restart", action="store_true")
    parser.add_argument("--skip-verify", action="store_true")
    parser.add_argument("--no-post-verify", action="store_true")
    options = parser.parse_args()
    if not options.host or options.host.startswith("-"):
        parser.error("缺少或无效的 QQBOT_HOST")
    if not options.key or not Path(options.key).is_file():
        parser.error("QQBOT_KEY 应指向本机 SSH 私钥文件")
    options.key = str(Path(options.key).resolve())
    try:
        publish(options)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"发布中断（{type(exc).__name__}）", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
