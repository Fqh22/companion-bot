#!/bin/bash
# cloud-healthcheck.sh —— 每 5 分钟检查云端机器人（NoneBot2 版），异常 QQ 私聊告警
set -uo pipefail

PY=/opt/nonebot/venv/bin/python
STATE=/opt/nonebot/ops/health.state
LOG=/opt/nonebot/ops/health.log
SNOW_API=http://127.0.0.1:3000
REPEAT_ALERT_SEC=21600

# 私有配置（管理员 QQ 等）放 ops.env，不入库。缺失时告警只写日志、不发送。
OPS_ENV=/opt/nonebot/ops/ops.env
[ -f "$OPS_ENV" ] && . "$OPS_ENV"
ADMIN="${ADMIN_QQ:-0}"

# OneBot 配置文件名带 QQ 号，改成通配，避免把号写死
if [ -z "${ONEBOT_CFG:-}" ]; then
    ONEBOT_CFG=$(ls /var/lib/docker/volumes/qq-gateway-data/_data/config/onebot_*.json 2>/dev/null | head -1)
fi

log() { printf '[%s] %s\n' "$(date '+%F %T')" "$*" >> "$LOG"; }
[ -f "$LOG" ] && [ "$(stat -c%s "$LOG")" -gt 1048576 ] && mv "$LOG" "$LOG.1"

TOKEN=$("$PY" - "$ONEBOT_CFG" 2>/dev/null <<'PY'
import json, sys
try:
    c = json.load(open(sys.argv[1], encoding="utf-8"))
    print(c["networks"]["httpServers"][0]["accessToken"])
except Exception:
    pass
PY
)

send() {
    local msg="$1"
    [ "${ADMIN:-0}" = "0" ] && { log "send skipped: admin not configured (ops.env)"; return 1; }
    [ -z "$TOKEN" ] && { log "send skipped: no onebot token"; return 1; }
    local js
    js=$("$PY" -c 'import json,sys;print(json.dumps({"user_id":int(sys.argv[1]),"message":sys.argv[2]},ensure_ascii=False))' "$ADMIN" "$msg") || return 1
    curl -s -m 10 -o /dev/null -w '%{http_code}' -X POST "$SNOW_API/send_private_msg" \
        -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d "$js" 2>/dev/null | grep -q '^200$'
}

if [ "${1:-}" = "--test-alert" ]; then
    if send "$(printf '🔧 告警通道测试\n%s' "$(date '+%m-%d %H:%M')")"; then
        echo "test-alert sent"; exit 0
    else
        echo "test-alert FAILED"; exit 1
    fi
fi

probs=()

systemctl is-active --quiet nonebot || probs+=("NoneBot 服务未运行")

resp=$(curl -s -m 8 -H "Authorization: Bearer $TOKEN" "$SNOW_API/get_login_info" 2>/dev/null || true)
if ! echo "$resp" | grep -q '"retcode":0'; then
    probs+=("QQ 未登录或 OneBot 无响应")
fi

use=$(df --output=pcent / 2>/dev/null | tail -1 | tr -dc '0-9')
[ "${use:-0}" -ge 90 ] && probs+=("磁盘使用 ${use}%")

avail=$(free -m | awk 'NR==2{print $7}')
[ "${avail:-0}" -le 80 ] && probs+=("可用内存仅 ${avail}MB")


# agentd：机器人能聊天不代表任务系统活着。它挂了的表现是 /task 没反应。
if ! curl -s -m 5 http://127.0.0.1:8100/health 2>/dev/null | grep -q '"ok": *true'; then
    probs+=("agentd 无响应")
fi

# 有消息没回：最后一条是用户消息且已经过了 15 分钟，说明回复链路卡住了。
# 命令类消息不入库，所以不会误报。
unanswered=$("$PY" - 2>/dev/null <<'PY'
import sqlite3, time
try:
    c = sqlite3.connect("file:/opt/nonebot/data/chat.db?mode=ro", uri=True)
    row = c.execute("select role, ts from messages order by id desc limit 1").fetchone()
    c.close()
    print(int(time.time() - float(row[1])) if row and row[0] == "user" else 0)
except Exception:
    print(0)
PY
)
if [ "${unanswered:-0}" -ge 900 ]; then
    probs+=("有消息 ${unanswered}s 没回")
fi

# 恢复演练：备份能不能用，只有真恢复才知道。超过 8 天没跑、或上次失败，都要说。
rs=$("$PY" - /opt/nonebot/ops/restore.state 2>/dev/null <<'PY'
import json, os, sys, time
p = sys.argv[1]
if not os.path.exists(p):
    print("missing"); raise SystemExit
try:
    d = json.load(open(p))
except Exception:
    print("bad"); raise SystemExit
if not d.get("ok"):
    print("failed")
elif time.time() - float(d.get("ts") or 0) > 8 * 86400:
    print("stale")
else:
    print("ok")
PY
)
case "$rs" in
    failed)  probs+=("上次恢复演练失败") ;;
    stale)   probs+=("恢复演练超过 8 天没跑") ;;
    missing) probs+=("从未做过恢复演练") ;;
    bad)     probs+=("恢复演练状态文件损坏") ;;
esac

# 异地备份：只报「配了但失败」和「状态异常」。
# unconfigured/missing 不告警——还没给桶的时候不该每 5 分钟响一次。
os_=$("$PY" - /opt/nonebot/ops/offsite.state 2>/dev/null <<'PY'
import json, os, sys
p = sys.argv[1]
if not os.path.exists(p):
    print("missing"); raise SystemExit
try:
    print(json.load(open(p)).get("status") or "bad")
except Exception:
    print("bad")
PY
)
case "$os_" in
    failed) probs+=("异地备份失败") ;;
    bad)    probs+=("异地备份状态异常") ;;
esac
now=$(date +%s)
fail_count=0; last_alert=0
if [ -f "$STATE" ]; then
    read -r fail_count last_alert < "$STATE" 2>/dev/null || true
    fail_count=${fail_count:-0}; last_alert=${last_alert:-0}
fi

if [ "${#probs[@]}" -eq 0 ]; then
    if [ "$fail_count" -ge 2 ]; then
        send "$(printf '✅ 云端机器人已恢复\n%s' "$(date '+%m-%d %H:%M')")" && log "recovered, alert sent" || log "recovered, alert send failed"
    fi
    echo "0 0" > "$STATE"
    log "ok"
    exit 0
fi

fail_count=$((fail_count + 1))
log "fail($fail_count): $(IFS='; '; echo "${probs[*]}")"

if [ "$fail_count" -ge 2 ] && [ $((now - last_alert)) -ge "$REPEAT_ALERT_SEC" ]; then
    body=$(printf '%s\n' "${probs[@]}" | sed 's/^/- /')
    msg=$(printf '⚠️ 云端机器人异常（连续 %s 次）\n%s\n%s' "$fail_count" "$body" "$(date '+%m-%d %H:%M')")
    send "$msg" && { last_alert=$now; log "alert sent"; } || log "alert send failed"
fi

echo "$fail_count $last_alert" > "$STATE"
exit 0
