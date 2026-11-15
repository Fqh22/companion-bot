#!/usr/bin/env bash
# OneBot 通道健康检查：SnowLuma 容器 + NoneBot + WS 端口。
# 连续 2 次失败才告警；ALERT_WEBHOOK 可选（POST JSON），没配就只写日志。
STATE_DIR="/var/lib/onebot-health"
FAIL_FILE="$STATE_DIR/fails"
ALERT_FILE="$STATE_DIR/last-alert"
LOG="/var/log/onebot-health.log"
mkdir -p "$STATE_DIR"
ts=$(date '+%F %T')
fails=0
[ -f "$FAIL_FILE" ] && fails=$(cat "$FAIL_FILE" 2>/dev/null || echo 0)
bad=""

if ! docker inspect -f '{{.State.Running}}' snowluma 2>/dev/null | grep -qx true; then
  bad="$bad snowluma容器未运行"
fi
if ! systemctl is-active --quiet nonebot; then
  bad="$bad nonebot未运行"
fi
if ! timeout 3 bash -c '</dev/tcp/127.0.0.1/3001' 2>/dev/null; then
  bad="$bad WS3001不可连"
fi

if [ -z "$bad" ]; then
  echo "$ts OK" >> "$LOG"
  echo 0 > "$FAIL_FILE"
  rm -f "$ALERT_FILE"
  exit 0
fi

fails=$((fails + 1))
echo "$fails" > "$FAIL_FILE"
msg="$ts 失败 $fails 次：$bad"
echo "$msg" >> "$LOG"
logger -t onebot-health "$msg" 2>/dev/null || true

last=0
[ -f "$ALERT_FILE" ] && last=$(cat "$ALERT_FILE" 2>/dev/null || echo 0)
now=$(date +%s)
if [ "$fails" -ge 2 ] && [ $((now - last)) -ge 1800 ]; then
  echo "$now" > "$ALERT_FILE"
  if [ -n "$ALERT_WEBHOOK" ]; then
    payload=$(printf '{"text":"[companion-bot] %s"}' "$msg")
    if curl -fsS --max-time 8 -X POST -H 'Content-Type: application/json' \
        -d "$payload" "$ALERT_WEBHOOK" >/dev/null 2>&1; then
      echo "$ts 已发 webhook" >> "$LOG"
    else
      echo "$ts webhook 发送失败" >> "$LOG"
    fi
  else
    echo "$ts 未配置 ALERT_WEBHOOK，只记录日志" >> "$LOG"
  fi
fi
exit 1
