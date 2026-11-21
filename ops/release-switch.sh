#!/usr/bin/env bash
# release-switch.sh —— 版本发布目录的原子切换。
#
# 布局：
#   /opt/nonebot-releases/<release>/   每个版本完整代码（含 ops）
#   /opt/nonebot/current -> ../nonebot-releases/<release>
#   /opt/nonebot/app_core -> current/app_core  （其余顶层代码同）
#   /opt/nonebot/.deployed_sha -> current/.deployed_sha
#   /opt/nonebot/{data,venv,config.yaml,.env}  仍是真实文件，不随版本走
#   /opt/nonebot-ops-state/            ops 日志 / state / ops.env 共享目录
#
# 这样切换 = 替换一个符号链接（rename，原子），不会出现半个版本；
# 回滚 = 把 current 指回旧 release，新版本独有的文件天然不会被带回来。
#
# 用法（root）：
#   release-switch.sh prepare <release-dir>   # 建 runtime 链接，供预检使用
#   release-switch.sh activate <release-id>   # 首次会迁移旧目录，然后原子切换 + 重启
#   release-switch.sh rollback <release-id>   # 切回旧 release + 重启
#   release-switch.sh status
set -euo pipefail

ROOT="${QQBOT_RELEASE_ROOT:-/opt}"
NB="$ROOT/nonebot"
RELEASES="$ROOT/nonebot-releases"
STATE="$ROOT/nonebot-ops-state"
SYSTEMCTL="${QQBOT_SYSTEMCTL:-systemctl}"
TS="$(date +%Y%m%d-%H%M%S)"
ENTRIES="app_core plugins adapters companion_core personas ops bot.py config.example.yaml requirements.txt README.md .env.example .deployed_sha"
MIGRATING=0
BAK=""

die() { echo "release-switch: $*" >&2; exit 1; }

runtime_globs() {
  printf "%s\n" "$NB"/ops/*.log "$NB"/ops/*.state "$NB"/ops/ops.env "$NB"/ops/.local-pull-ts
}

# 把旧目录里的 runtime 文件复制到共享 state（幂等）。
seed_state() {
  [ -d "$NB/ops" ] && [ ! -L "$NB/ops" ] || return 0
  mkdir -p "$STATE"
  for f in "$NB"/ops/*.log "$NB"/ops/*.state "$NB"/ops/ops.env "$NB"/ops/.local-pull-ts; do
    [ -f "$f" ] || continue
    b="$(basename "$f")"
    [ -e "$STATE/$b" ] || cp -a "$f" "$STATE/$b"
  done
}

# 把一个 release 的 ops runtime 文件指向共享 state。
link_runtime() {
  rel="$1"
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

restore_legacy() {
  [ -n "$BAK" ] && [ -d "$BAK" ] || return 0
  echo "release-switch: 迁移失败，恢复旧目录 $BAK" >&2
  for e in $ENTRIES; do rm -f "$NB/$e"; done
  for e in $ENTRIES; do [ -e "$BAK/$e" ] && mv "$BAK/$e" "$NB/$e"; done
  "$SYSTEMCTL" start nonebot 2>/dev/null || true
}
trap 'if [ "$MIGRATING" = 1 ]; then restore_legacy; fi' ERR

prepare() {
  rel="$1"
  [ -d "$rel" ] || die "release 不存在：$rel"
  seed_state
  link_runtime "$rel"
  ln -sfn "$NB/venv" "$rel/venv"   # 只给 verify --code 借解释器
}

# 第一次从「散装目录」迁到 release 布局。会短暂停服务。
migrate() {
  id="$1"; rel="$RELEASES/$id"
  [ -d "$rel" ] || die "release 不存在：$rel"
  seed_state
  old_sha="$(cat "$NB/.deployed_sha" 2>/dev/null | tr -d "[:space:]" || true)"
  leg="$RELEASES/legacy-${old_sha:-unknown}"
  mkdir -p "$leg"
  for e in $ENTRIES; do [ -e "$NB/$e" ] && cp -a "$NB/$e" "$leg/$e"; done
  for f in "$leg"/ops/*.log "$leg"/ops/*.state "$leg"/ops/ops.env "$leg"/ops/.local-pull-ts; do
    [ -f "$f" ] || continue
    b="$(basename "$f")"
    [ -e "$STATE/$b" ] || mv "$f" "$STATE/$b"
  done
  rm -f "$leg"/ops/*.log "$leg"/ops/*.state "$leg"/ops/ops.env "$leg"/ops/.local-pull-ts 2>/dev/null || true
  link_runtime "$leg"
  ln -sfn "$NB/venv" "$leg/venv"
  printf "%s\n" "$old_sha" > "$leg/.deployed_sha"
  echo "release-switch: 首次迁移，旧版本保留为 $leg"
  MIGRATING=1
  "$SYSTEMCTL" stop nonebot 2>/dev/null || true
  BAK="$NB/legacy-$TS"
  mkdir -p "$BAK"
  for e in $ENTRIES; do [ -e "$NB/$e" ] && mv "$NB/$e" "$BAK/$e"; done
  for e in $ENTRIES; do ln -sfn "current/$e" "$NB/$e"; done
  ln -sfn "../nonebot-releases/$id" "$NB/.current.tmp"
  mv -T "$NB/.current.tmp" "$NB/current"
  "$SYSTEMCTL" start nonebot
  MIGRATING=0
  sleep 3
  "$SYSTEMCTL" is-active nonebot >/dev/null || die "迁移后服务未 active"
}

activate() {
  id="$1"; rel="$RELEASES/$id"
  [ -d "$rel" ] || die "release 不存在：$rel"
  if [ ! -L "$NB/current" ]; then
    migrate "$id"
    return 0
  fi
  link_runtime "$rel"
  ln -sfn "../nonebot-releases/$id" "$NB/.current.tmp"
  mv -T "$NB/.current.tmp" "$NB/current"
  "$SYSTEMCTL" restart nonebot
  sleep 3
  "$SYSTEMCTL" is-active nonebot >/dev/null || die "切换后服务未 active（可 rollback）"
}

rollback() {
  id="$1"
  [ -d "$RELEASES/$id" ] || die "回滚目标不存在：$RELEASES/$id"
  [ -L "$NB/current" ] || die "当前还是散装布局，不能 rollback"
  ln -sfn "../nonebot-releases/$id" "$NB/.current.tmp"
  mv -T "$NB/.current.tmp" "$NB/current"
  "$SYSTEMCTL" restart nonebot
  sleep 3
  "$SYSTEMCTL" is-active nonebot >/dev/null || die "回滚后服务未 active"
  echo "release-switch: 已回滚到 $id"
}

cmd="${1:-}"
case "$cmd" in
  prepare) prepare "${2:?release-dir}" ;;
  activate) activate "${2:?release-id}" ;;
  rollback) rollback "${2:?release-id}" ;;
  status)
    echo "current=$(readlink "$NB/current" 2>/dev/null || echo none)"
    echo "deployed=$(cat "$NB/.deployed_sha" 2>/dev/null || echo none)"
    echo "releases:"; ls -1 "$RELEASES" 2>/dev/null | tail -10 || true
    ;;
  -h|--help) sed -n "2,20p" "$0" | sed "s/^# \{0,1\}//" ;;
  *) die "未知命令：$cmd" ;;
esac
