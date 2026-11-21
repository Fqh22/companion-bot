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
# 缺库不能静默跳过：以前 continue 会产出「看着成功、其实没记忆」的备份，
# 恢复演练再严也救不回来。必需库不在就直接失败。
for db in /opt/qqbot-data/livingmemory.db /opt/nonebot/data/chat.db; do
    [ -f "$db" ] || die "缺少必需库: $db"
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
# data/files 是消息里的媒体缓存（2026-11-15 实测 1.7MB/15 个，可重放、会长大），
# 不进备份；要留原文就删掉下面的 --exclude='files'。
tar -C /opt/nonebot/data -cf - \
    --exclude='*.db' --exclude='*.db-wal' --exclude='*.db-shm' \
    --exclude='files' . | tar -C "$STAGE/nonebot-data" -xf - || die "tar nonebot-data"

# 4) 配置与代码（不含 venv）
tar -C /opt/nonebot -cf - \
    --exclude='venv' --exclude='__pycache__' --exclude='data' --exclude='ops' \
    bot.py .env config.yaml app_core plugins 2>/dev/null \
    | tar -C "$STAGE/code" -xf - || log "warn: code tar"
cp /etc/systemd/system/nonebot.service "$STAGE/nonebot.service" 2>/dev/null || true
# ops 下的入口脚本与运维 python 都进备份：云端有些 .py 只在机器上（export-*/probe-*），
# 不备份的话重装/恢复演练会丢。日志、state、ops.env（含 webhook 等配置）明确不备。
cp -a /opt/nonebot/ops/*.sh /opt/nonebot/ops/*.py "$STAGE/ops/" 2>/dev/null || true

# 4.5) 写清单：恢复演练据此核对「文件全不全、行数对不对」。
# 没有清单的备份只能证明「能解压」，证明不了「解出来是这份数据」。
"$PY" - "$STAGE" <<'PY' || die "manifest"
import hashlib, json, sqlite3, sys, time
from pathlib import Path

stage = Path(sys.argv[1])
required = {
    "sqlite/qqbot-data/livingmemory.db": ("documents", "memory_atoms"),
    "sqlite/nonebot/data/chat.db": ("messages",),
}
files, dbs = {}, {}
for p in sorted(stage.rglob("*")):
    if p.is_file():
        rel = p.relative_to(stage).as_posix()
        blob = p.read_bytes()
        files[rel] = {"sha256": hashlib.sha256(blob).hexdigest(), "bytes": len(blob)}
for rel, tables in required.items():
    p = stage / rel
    if not p.is_file():
        print("missing required db: " + rel, file=sys.stderr)
        sys.exit(1)
    c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    counts = {}
    for t in tables:
        try:
            counts[t] = c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        except Exception:
            counts[t] = -1
    c.close()
    dbs[rel] = {"tables": counts}
(stage / "manifest.json").write_text(json.dumps({
    "format": 2, "created_at": time.time(), "files": files, "dbs": dbs,
}, ensure_ascii=False), encoding="utf-8")
PY

# 5) 打包 + 校验 + 轮转
tar -C "$STAGE" -czf "$OUT" . || die "tar pack"
tar -tzf "$OUT" >/dev/null 2>&1 || die "tar verify"
rm -rf "$STAGE"
find "$BK" -maxdepth 1 -name 'cloud-*.tgz' -mtime +"$KEEP_DAYS" -delete 2>/dev/null
log "ok: $(basename "$OUT") ($(du -h "$OUT" | cut -f1))"
echo "$OUT"
