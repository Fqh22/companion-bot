#!/usr/bin/env bash
# release-switch.sh —— 整目录原子切换发布版本。
#
# 布局：
#   /opt/nonebot                     当前跑的那份完整发布（代码 + ops）
#   /opt/nonebot-releases/<id>/       历史版本，可随时切回
#   /opt/nonebot-shared/              data / venv / config.yaml / .env（不随版本走）
#   /opt/nonebot-ops-state/           ops 日志 / state / ops.env（不随版本走）
#
# /opt/nonebot 是真实目录，插件路径仍在它下面（NoneBot 用 resolve()
# 算模块名，符号链接目录会算错）。切换用 renameat2(RENAME_EXCHANGE)
# 一次交换两个目录；服务在交换前停、交换后启，不会出现新旧混合。
# 每个版本在 releases/ 留一份硬链接快照，回滚后仍保留回滚目标。
#
# 用法（root）：
#   release-switch.sh prepare <release-dir>
#   release-switch.sh activate <release-dir> <release-id>
#   release-switch.sh rollback <release-id>
#   release-switch.sh status
set -euo pipefail

ROOT="${QQBOT_RELEASE_ROOT:-/opt}"
NB="$ROOT/nonebot"
RELEASES="$ROOT/nonebot-releases"
SHARED="$ROOT/nonebot-shared"
STATE="$ROOT/nonebot-ops-state"
SYSTEMCTL="${QQBOT_SYSTEMCTL:-systemctl}"
TS="$(date +%Y%m%d-%H%M%S)"

die() { echo "release-switch: $*" >&2; exit 1; }
trap '"$SYSTEMCTL" start nonebot 2>/dev/null || true' ERR

exchange() {
  python3 - "$1" "$2" <<'PY'
import ctypes, os, sys
a, b = sys.argv[1].encode(), sys.argv[2].encode()
libc = ctypes.CDLL(None, use_errno=True)
try:
    r = libc.renameat2(-100, a, -100, b, 2)
except AttributeError:
    r = libc.syscall(316, -100, a, -100, b, 2)
if r != 0:
    raise SystemExit('renameat2: ' + os.strerror(ctypes.get_errno()))
PY
}

link_shared() {
  local rel="$1" n
  for n in data venv config.yaml .env; do
    if [ -e "$SHARED/$n" ]; then
      ln -sfn "$SHARED/$n" "$rel/$n"
    elif [ -e "$NB/$n" ]; then
      ln -sfn "$NB/$n" "$rel/$n"
    fi
  done
  return 0
}

seed_state() {
  local f b
  mkdir -p "$STATE"
  if [ -d "$NB/ops" ] && [ ! -L "$NB/ops" ]; then
    for f in "$NB"/ops/*.log "$NB"/ops/*.state "$NB"/ops/ops.env "$NB"/ops/.local-pull-ts; do
      [ -f "$f" ] || continue
      b="$(basename "$f")"
      [ -e "$STATE/$b" ] || cp -a "$f" "$STATE/$b"
    done
  fi
}

link_runtime() {
  local rel="$1" f n
  [ -d "$rel/ops" ] || return 0
  mkdir -p "$STATE"
  for f in "$STATE"/*.log "$STATE"/*.state; do
    [ -e "$f" ] || continue
    ln -sfn "$f" "$rel/ops/$(basename "$f")"
  done
  for n in ops.env .local-pull-ts; do
    [ -e "$STATE/$n" ] && ln -sfn "$STATE/$n" "$rel/ops/$n"
  done
  return 0
}

# 停服务前等一等正在生成的那一轮：用户刚发消息、助手还没回时，
# 直接 stop 会把已经生成好的回复截掉（2026-11-22 01:38 实际发生过）。
wait_idle() {
  local max="${1:-60}" waited=0 state db="$NB/data/chat.db" inflight="$NB/data/inflight.json"
  while [ "$waited" -lt "$max" ]; do
    state="$(python3 - "$db" "$inflight" <<'PY'
import json, os, sqlite3, sys, time
db, inflight = sys.argv[1], sys.argv[2]
now = time.time()
busy = False
# 用户消息要到生成结束后才落库。在途文件才是这一轮是否还在跑的真实状态。
try:
    if os.path.isfile(inflight):
        data = json.load(open(inflight, encoding="utf-8"))
        if isinstance(data, dict):
            for sid, rec in data.items():
                if str(sid).startswith("group:"):
                    continue
                if not isinstance(rec, dict):
                    continue
                age = now - float(rec.get("ts") or 0)
                pid = int(rec.get("pid") or 0)
                alive = False
                if pid > 0:
                    try:
                        os.kill(pid, 0)
                        alive = True
                    except OSError:
                        alive = False
                if alive and 0 <= age < 180:
                    busy = True
                    break
except Exception:
    pass
if not busy and os.path.isfile(db):
    try:
        c = sqlite3.connect("file:" + db + "?mode=ro", uri=True)
        row = c.execute(
            "SELECT "
            " (SELECT MAX(ts) FROM messages WHERE role='user' AND session NOT LIKE 'group:%'),"
            " (SELECT MAX(ts) FROM messages WHERE role='assistant' AND session NOT LIKE 'group:%')"
        ).fetchone()
        c.close()
        u, a = float(row[0] or 0), float(row[1] or 0)
        # 只拦「刚刚发的、还在生成」的那轮；几十分钟前的未回消息不该拖住部署。
        if u > a and now - u < 120:
            busy = True
    except Exception:
        pass
print("busy" if busy else "idle")
PY
)"
    [ "$state" = "idle" ] && return 0
    echo "release-switch: 当前一轮还在生成，等待收尾（已等 ${waited}s）" >&2
    sleep 2
    waited=$((waited + 2))
  done
  echo "release-switch: 等待超时，继续切换" >&2
  return 0
}

prepare() {
  local rel="$1"
  [ -d "$rel" ] || die "release 不存在：$rel"
  link_shared "$rel"
  link_runtime "$rel"
}

archive_live() {
  local aid="$1" dst="$RELEASES/$1"
  # 只有带完整标记的目录才算可回滚的版本；失败实验留下的半成品要重建。
  if [ -f "$dst/.deployed_sha" ] && [ -f "$dst/.release_id" ]; then return 0; fi
  [ -e "$dst" ] && rm -rf "$dst"
  cp -al "$NB" "$dst"
}

swap_in() {
  local can="$1" swap="$RELEASES/.swap-$TS-$$"
  link_shared "$can"
  link_runtime "$can"
  cp -al "$can" "$swap"
  exchange "$NB" "$swap"
  rm -rf "$swap"
}

migrate() {
  local can="$1" id="$2" old_id leg
  old_id="legacy-$(cat "$NB/.deployed_sha" 2>/dev/null | tr -d "[:space:]" || echo unknown)"
  echo "release-switch: 首次迁移，旧版本保留为 $RELEASES/$old_id"
  wait_idle 60
  "$SYSTEMCTL" stop nonebot 2>/dev/null || true
  mkdir -p "$SHARED" "$RELEASES" "$STATE"
  for n in data venv config.yaml .env; do
    if [ -e "$NB/$n" ] && [ ! -L "$NB/$n" ]; then mv "$NB/$n" "$SHARED/$n"; fi
  done
  seed_state
  printf "%s\n" "$old_id" > "$NB/.release_id"
  link_shared "$NB"
  link_runtime "$NB"
  archive_live "$old_id"
  printf "%s\n" "$id" > "$can/.release_id"
  swap_in "$can"
  if ! "$SYSTEMCTL" start nonebot; then
    echo "release-switch: 迁移后启动失败，切回旧版本" >&2
    swap_in "$RELEASES/$old_id"
    "$SYSTEMCTL" start nonebot 2>/dev/null || true
    die "迁移后服务启动失败"
  fi
  sleep 3
  if ! "$SYSTEMCTL" is-active nonebot >/dev/null; then
    swap_in "$RELEASES/$old_id"
    "$SYSTEMCTL" start nonebot 2>/dev/null || true
    die "迁移后服务未 active"
  fi
}

activate() {
  local can="$1" id="$2" old_id
  [ -d "$can" ] || die "release 不存在：$can"
  if [ ! -d "$SHARED" ] || { [ -e "$NB/data" ] && [ ! -L "$NB/data" ]; }; then
    migrate "$can" "$id"
    return 0
  fi
  old_id="$(cat "$NB/.release_id" 2>/dev/null || echo old-$TS)"
  printf "%s\n" "$id" > "$can/.release_id"
  wait_idle 60
  "$SYSTEMCTL" stop nonebot 2>/dev/null || true
  archive_live "$old_id"
  swap_in "$can"
  if ! "$SYSTEMCTL" start nonebot; then
    swap_in "$RELEASES/$old_id"
    "$SYSTEMCTL" start nonebot 2>/dev/null || true
    die "新版本启动失败，已切回 $old_id"
  fi
  sleep 3
  if ! "$SYSTEMCTL" is-active nonebot >/dev/null; then
    swap_in "$RELEASES/$old_id"
    "$SYSTEMCTL" start nonebot 2>/dev/null || true
    die "新版本未 active，已切回 $old_id"
  fi
  echo "release-switch: 已切换到 $id（旧版本 $old_id 保留）"
}

rollback() {
  local id="$1" tgt="$RELEASES/$1" cur
  [ -d "$tgt" ] || die "回滚目标不存在：$tgt"
  cur="$(cat "$NB/.release_id" 2>/dev/null || echo current-$TS)"
  wait_idle 60
  "$SYSTEMCTL" stop nonebot 2>/dev/null || true
  archive_live "$cur"
  swap_in "$tgt"
  if ! "$SYSTEMCTL" start nonebot; then die "回滚后服务启动失败，旧当前版本在 $RELEASES/$cur"; fi
  sleep 3
  if ! "$SYSTEMCTL" is-active nonebot >/dev/null; then die "回滚后服务未 active，旧当前版本在 $RELEASES/$cur"; fi
  echo "release-switch: 已回滚到 $id（之前版本 $cur 保留）"
}

cmd="${1:-}"
case "$cmd" in
  prepare) prepare "${2:?release-dir}" ;;
  activate) activate "${2:?release-dir}" "${3:?release-id}" ;;
  rollback) rollback "${2:?release-id}" ;;
  status)
    echo "release=$(cat "$NB/.release_id" 2>/dev/null || echo unknown)"
    echo "deployed=$(cat "$NB/.deployed_sha" 2>/dev/null || echo none)"
    echo "releases:"; ls -1 "$RELEASES" 2>/dev/null | tail -10 || true
    ;;
  -h|--help) sed -n "2,18p" "$0" | sed "s/^# \{0,1\}//" ;;
  *) die "未知命令：$cmd" ;;
esac
