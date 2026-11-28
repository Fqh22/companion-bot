#!/usr/bin/env bash
# SSH 主机和本机密钥用 QQBOT_HOST / QQBOT_KEY 配置。
set -euo pipefail
if command -v python3 >/dev/null 2>&1; then
    exec python3 "$(dirname "$0")/deploy.py" "$@"
fi
exec python "$(dirname "$0")/deploy.py" "$@"
