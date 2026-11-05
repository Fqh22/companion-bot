#!/bin/bash
# verify.sh —— 一键跑「六件套」，任何一套失败就返回非零。
#
# 用法（在云端 /opt/nonebot 下，root 或 ubuntu 都行）：
#   ops/verify.sh            # 全量：分层 + 离线回归 + 联动体检 + 全链路 + 插件启动
#   ops/verify.sh --pre      # 重启前：分层 + 离线回归 + 联动体检（都不碰生产库、不发消息）
#   ops/verify.sh --quick    # 秒级：分层 + 联动体检（改完想先看一眼接线时用）
#   ops/verify.sh --verbose  # 失败时也打印完整输出
#   ops/verify.sh --code /path/to/nonebot
#
# 退出码：0 全过；1 有失败；2 环境不对（找不到代码或解释器）。
set -uo pipefail

CODE="${QQBOT_CODE_DIR:-/opt/nonebot}"
PY="$CODE/venv/bin/python"
MODE="all"
VERBOSE=0

while [ $# -gt 0 ]; do
    case "$1" in
        --pre)     MODE="pre" ;;
        --quick)   MODE="quick" ;;
        --all)     MODE="all" ;;
        --verbose|-v) VERBOSE=1 ;;
        --code)    shift; CODE="$1"; PY="$CODE/venv/bin/python" ;;
        -h|--help)
            sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *) echo "未知参数：$1（用 --help 看用法）" >&2; exit 2 ;;
    esac
    shift
done

OPS="$CODE/ops"
[ -x "$PY" ] || { echo "找不到解释器：$PY" >&2; exit 2; }
[ -d "$OPS" ] || { echo "找不到 ops 目录：$OPS" >&2; exit 2; }

# 云端是 root，但测试要用 ubuntu 跑（生产库只读副本、写临时目录）。
# 本地/切换用户后已经是 ubuntu，就别再 sudo。
if [ "$(id -u)" = "0" ]; then
    RUN=(sudo -u ubuntu -E)
else
    RUN=()
fi

TOTAL=0
FAILED=0
declare -a NAMES=()
declare -a STATS=()
declare -a DUR=()

run_suite() {
    local name="$1" script="$2"
    [ -f "$script" ] || { echo "跳过（缺文件）：$name"; return 0; }

    TOTAL=$((TOTAL + 1))
    local t0 t1 out rc
    t0=$(date +%s%N)
    out=$("${RUN[@]}" env PYTHONIOENCODING=utf-8 QQBOT_CODE_DIR="$CODE" \
          DRIVER='~websockets' "$PY" "$script" 2>&1)
    rc=$?
    t1=$(date +%s%N)
    local secs
    secs=$(awk "BEGIN{printf \"%.1f\", ($t1-$t0)/1000000000}")

    if [ "$rc" -eq 0 ]; then
        NAMES+=("$name"); STATS+=("PASS"); DUR+=("${secs}s")
        printf '  %-10s PASS  %6ss\n' "$name" "$secs"
    else
        NAMES+=("$name"); STATS+=("FAIL"); DUR+=("${secs}s")
        FAILED=$((FAILED + 1))
        printf '  %-10s FAIL  %6ss\n' "$name" "$secs"
        # 失败时打印关键行，别让人再翻一遍
        if [ "$VERBOSE" -eq 1 ]; then
            echo "$out" | sed 's/^/      /'
        else
            key=$(echo "$out" | grep -E "^(FAIL|失败项|  - )" | head -12)
            # 像 lint 这种输出格式不同的，退回「最后十几行」
            [ -z "$key" ] && key=$(echo "$out" | tail -14)
            echo "$key" | sed 's/^/      /'
        fi
    fi
}

echo "=== 验证（模式：$MODE，代码：$CODE）==="

case "$MODE" in
    quick)
        run_suite "分层检查" "$OPS/lint-plugins.py"
        run_suite "联动体检" "$OPS/link-check.py"
        ;;
    pre)
        run_suite "分层检查" "$OPS/lint-plugins.py"
        run_suite "离线回归" "$OPS/offline-test.py"
        run_suite "联动体检" "$OPS/link-check.py"
        ;;
    *)
        run_suite "分层检查" "$OPS/lint-plugins.py"
        run_suite "离线回归" "$OPS/offline-test.py"
        run_suite "联动体检" "$OPS/link-check.py"
        run_suite "全链路"   "$OPS/smoke-all.py"
        run_suite "插件启动" "$OPS/smoke-plugins.py"
        ;;
esac

# agentd 自带测试：不在 /opt/nonebot 下，用自己的 venv 和 cwd。
# 以前只写在文档里「agentd 自带 68 项」——测试存在不等于测试在跑。
# 2026-11-05 实证：planner 里一个未定义变量让 /task 规划整条挂掉，
# 因为在五套件之外，上线一天没人发现。
run_agentd() {
    local ad="${AGENTD_DIR:-/opt/agentd}"
    if [ ! -d "$ad/agentd/tests" ] || [ ! -x "$ad/venv/bin/python" ]; then
        echo "  agentd     SKIP  没有 $ad（agentd 和机器人可能不在一台机器上）"
        return 0
    fi
    TOTAL=$((TOTAL + 1))
    local t0 t1 out rc secs
    t0=$(date +%s%N)
    out=$(cd "$ad" && PYTHONPATH="$ad" "$ad/venv/bin/python" agentd/tests/test_core.py 2>&1)
    rc=$?
    t1=$(date +%s%N)
    secs=$(awk "BEGIN{printf \"%.1f\", ($t1-$t0)/1000000000}")
    if [ "$rc" -eq 0 ]; then
        NAMES+=("agentd"); STATS+=("PASS"); DUR+=("${secs}s")
        printf '  %-10s PASS  %6ss\n' "agentd" "$secs"
    else
        NAMES+=("agentd"); STATS+=("FAIL"); DUR+=("${secs}s")
        FAILED=$((FAILED + 1))
        printf '  %-10s FAIL  %6ss\n' "agentd" "$secs"
        if [ "$VERBOSE" -eq 1 ]; then
            echo "$out" | sed 's/^/      /'
        else
            echo "$out" | tail -12 | sed 's/^/      /'
        fi
    fi
}

if [ "$MODE" != "quick" ]; then
    run_agentd
fi

echo
if [ "$FAILED" -eq 0 ]; then
    echo "=== 全部通过（$TOTAL/$TOTAL）==="
    exit 0
fi
echo "=== 有失败：$((TOTAL - FAILED))/$TOTAL 通过 ==="
exit 1
