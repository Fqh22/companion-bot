#!/bin/bash
# offsite-backup.sh —— 把最新的整机备份推到异地。
#
# 为什么要有：/opt/backup 和数据在同一块盘、同一台机器上，盘挂了备份一起没。
#
# 配置（/opt/nonebot/ops/ops.env，600，不入库）：
#   方式一（通用）：BACKUP_PUSH_CMD='rclone copy {file} cos:qqbot-backup/'
#                    BACKUP_PRUNE_CMD='rclone delete cos:qqbot-backup --min-age 30d'（可选）
#   方式二：装了 rclone + BACKUP_REMOTE=cos:qqbot-backup
#   方式三：装了 coscli + BACKUP_BUCKET=cos://bucket/path
#
# 没配置时**不算失败**，但会把 status=unconfigured 写进 state，/ping 和健康检查看得到。
set -uo pipefail

BACKUP_DIR=${BACKUP_DIR:-/opt/backup}
STATE=/opt/nonebot/ops/offsite.state
LOG=/opt/nonebot/ops/offsite.log
OPS_ENV=/opt/nonebot/ops/ops.env
[ -f "$OPS_ENV" ] && . "$OPS_ENV"

log() { printf '[%s] %s\n' "$(date '+%F %T')" "$*" >> "$LOG"; }

save_state() {
    # $1 = json 对象
    printf '%s\n' "$1" > "$STATE" 2>/dev/null || true
}

file=$(ls -t "$BACKUP_DIR"/cloud-*.tgz 2>/dev/null | head -1)
if [ -z "${file:-}" ]; then
    log "没有可上传的备份"
    save_state "{\"status\":\"failed\",\"error\":\"no backup\",\"ts\":$(date +%s)}"
    exit 1
fi
name=$(basename "$file")
size=$(du -h "$file" | cut -f1)

uploaded=0
if [ -n "${BACKUP_PUSH_CMD:-}" ]; then
    cmd=${BACKUP_PUSH_CMD//\{file\}/$file}
    cmd=${cmd//\{name\}/$name}
    log "按 BACKUP_PUSH_CMD 上传：$cmd"
    if eval "$cmd" >> "$LOG" 2>&1; then uploaded=1; else log "上传失败：$cmd"; fi
elif command -v rclone >/dev/null 2>&1 && [ -n "${BACKUP_REMOTE:-}" ]; then
    log "rclone → $BACKUP_REMOTE"
    if rclone copy "$file" "$BACKUP_REMOTE" >> "$LOG" 2>&1; then uploaded=1; else log "rclone 上传失败"; fi
elif command -v coscli >/dev/null 2>&1 && [ -n "${BACKUP_BUCKET:-}" ]; then
    log "coscli → $BACKUP_BUCKET"
    if coscli cp "$file" "$BACKUP_BUCKET/$name" >> "$LOG" 2>&1; then uploaded=1; else log "coscli 上传失败"; fi
fi

if [ "$uploaded" = "1" ]; then
    log "异地备份成功：$name ($size)"
    save_state "{\"status\":\"ok\",\"file\":\"$name\",\"ts\":$(date +%s)}"
    if [ -n "${BACKUP_PRUNE_CMD:-}" ]; then
        log "清理远端旧备份：$BACKUP_PRUNE_CMD"
        eval "$BACKUP_PRUNE_CMD" >> "$LOG" 2>&1 || log "远端清理失败（不影响本次上传）"
    fi
    exit 0
fi

if [ -z "${BACKUP_PUSH_CMD:-}${BACKUP_REMOTE:-}${BACKUP_BUCKET:-}" ]; then
    log "未配置异地备份：在 $OPS_ENV 里设 BACKUP_PUSH_CMD（或 BACKUP_REMOTE/rclone、BACKUP_BUCKET/coscli）"
    save_state "{\"status\":\"unconfigured\",\"ts\":$(date +%s)}"
    exit 0
fi

save_state "{\"status\":\"failed\",\"file\":\"$name\",\"ts\":$(date +%s)}"
exit 1
