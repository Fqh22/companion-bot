#!/bin/bash
# cloud-backup.sh —— 每日快照（NoneBot 版）
# 内容：/opt/qqbot-data（workspace、记忆库、密钥、表情包）+ /opt/nonebot/data + 配置
set -uo pipefail

BK=/opt/backup
KEEP_DAYS=7
LOG=/opt/nonebot/ops/backup.log
PY=/opt/nonebot/venv/bin/python
TS=$(date +%Y%m%d-%H%M%S)
STAGE="$BK/.stage-$TS"
OUT="$BK/cloud-$TS.tgz"

log() { printf '[%s] %s\n' "$(date '+%F %T')" "$*" >> "$LOG"; }
die() { log "FAILED: $*"; rm -rf "$STAGE"; exit 1; }

exec 9>/var/lock/cloud-backup.lock
flock -n 9 || { log "another backup is running, skip"; exit 0; }

mkdir -p "$BK" /opt/nonebot/ops || exit 1
rm -rf "$STAGE"
mkdir -p "$STAGE/sqlite" "$STAGE/qqbot-data" "$STAGE/nonebot-data" "$STAGE/code" "$STAGE/ops" || die "mkdir stage"
log "=== backup start ($TS) ==="

# 1) SQLite 一致性导出
for db in /opt/qqbot-data/livingmemory.db /opt/nonebot/data/chat.db /opt/agentd/data/agentd.db; do
    [ -f "$db" ] || continue
    rel="${db#/opt/}"
    dst="$STAGE/sqlite/$rel"
    mkdir -p "$(dirname "$dst")"
    "$PY" - "$db" "$dst" <<'PY' || die "sqlite export: $db"
import sqlite3, sys
src, dst = sys.argv[1], sys.argv[2]
s = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
d = sqlite3.connect(dst)
with d:
    s.backup(d)
d.close(); s.close()
PY
done

# 2) qqbot-data（排除原 db，已单独导出）
tar -C /opt/qqbot-data -cf - \
    --exclude='*.db' --exclude='*.db-wal' --exclude='*.db-shm' \
    --exclude='meme_packs' \
    . | tar -C "$STAGE/qqbot-data" -xf - || die "tar qqbot-data"

# 3) nonebot data（排除 db）
tar -C /opt/nonebot/data -cf - \
    --exclude='*.db' --exclude='*.db-wal' --exclude='*.db-shm' \
    --exclude='files' . | tar -C "$STAGE/nonebot-data" -xf - || die "tar nonebot-data"

# 4) 配置与代码（不含 venv）
tar -C /opt/nonebot -cf - \
    --exclude='venv' --exclude='__pycache__' --exclude='data' --exclude='ops' \
    bot.py .env config.yaml app_core plugins 2>/dev/null \
    | tar -C "$STAGE/code" -xf - || log "warn: code tar"
cp /etc/systemd/system/nonebot.service "$STAGE/nonebot.service" 2>/dev/null || true
cp -a /opt/nonebot/ops/*.sh "$STAGE/ops/" 2>/dev/null || true

# 5) agentd：计划 + 报告 + 项目状态（都是几十 KB，产物不备份）
if [ -d /opt/agentd/work ]; then
    mkdir -p "$STAGE/agentd-work"
    ( cd /opt/agentd/work \
        && find . -maxdepth 1 -name 'PLAN.md' -o -maxdepth 1 -name 'report.md' -o -maxdepth 3 -path './projects/*/STATE.md' ) \
        | tar -C /opt/agentd/work -cf - -T - 2>/dev/null \
        | tar -C "$STAGE/agentd-work" -xf - || log "warn: agentd work tar"
fi


# 6) 打包 + 校验 + 轮转
tar -C "$STAGE" -czf "$OUT" . || die "tar pack"
tar -tzf "$OUT" >/dev/null 2>&1 || die "tar verify"
rm -rf "$STAGE"
find "$BK" -maxdepth 1 -name 'cloud-*.tgz' -mtime +"$KEEP_DAYS" -delete 2>/dev/null
log "ok: $(basename "$OUT") ($(du -h "$OUT" | cut -f1))"
echo "$OUT"
