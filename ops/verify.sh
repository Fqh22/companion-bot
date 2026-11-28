#!/usr/bin/env bash
# 云端入口，使用候选或生产版本自带的验证清单。
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
CODE="${QQBOT_CODE_DIR:-/opt/nonebot}"
MODE=all
EXTRA=()
while [ "$#" -gt 0 ]; do
    case "$1" in
        --pre|--quick|--all) MODE="${1#--}" ;;
        --code) shift; CODE="${1:?--code 需要目录}" ;;
        --verbose|-v) EXTRA+=(--verbose) ;;
        -h|--help) echo "用法：ops/verify.sh [--quick|--pre|--all] [--code PATH] [--verbose]"; exit 0 ;;
        *) echo "未知参数：$1" >&2; exit 2 ;;
    esac
    shift
done
PY="$CODE/venv/bin/python"
[ -x "$PY" ] || { echo "找不到解释器：$PY" >&2; exit 2; }
if [ "$(id -u)" = 0 ]; then
    exec sudo -u ubuntu -E "$PY" "$HERE/verify.py" --profile "$MODE" --code "$CODE" "${EXTRA[@]}"
fi
exec "$PY" "$HERE/verify.py" --profile "$MODE" --code "$CODE" "${EXTRA[@]}"
