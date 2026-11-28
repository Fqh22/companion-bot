#!/usr/bin/env bash
# 部署：构建 release → 候选验证 → 原子切换 current → 重启。
#
# 布局由 ops/release-switch.sh 维护：
#   /opt/nonebot-releases/<sha>-<ts>/   每个版本完整代码
#   /opt/nonebot 是真实目录，由 release-switch.sh 用 renameat2 原子交换
#   /opt/nonebot/{data,venv,config.yaml,.env} 保持真实，不随版本走
#   /opt/nonebot-ops-state/              ops 日志/state/ops.env 共享
#
# 旧流程先删旧文件再 tar 覆盖，中途失败会留下混合版本；现在切换是一个
# rename，失败时 current 仍指向旧 release。
#
# 用法：
#   export QQBOT_HOST=ubuntu@<server>      # 含用户名
#   export QQBOT_KEY=/path/to/key.pem
#   bash ops/deploy.sh                     # 全流程
#   bash ops/deploy.sh --no-restart        # 只构建+验证，不切换
#   bash ops/deploy.sh --skip-verify       # 跳过候选验证（紧急回滚用）
#   bash ops/deploy.sh --no-post-verify    # 重启后不跑全量五件套
set -euo pipefail

HOST="${QQBOT_HOST:?请先 export QQBOT_HOST（含用户名，如 ubuntu@host）}"
KEY="${QQBOT_KEY:?请先 export QQBOT_KEY 指向 ssh 私钥}"
RESTART=1
PRE_VERIFY=1
POST_VERIFY=1
for a in "$@"; do
  case "$a" in
    --no-restart) RESTART=0 ;;
    --skip-verify) PRE_VERIFY=0 ;;
    --no-post-verify) POST_VERIFY=0 ;;
    -h|--help) sed -n "2,25p" "$0" | sed "s/^# \{0,1\}//"; exit 0 ;;
    *) echo "未知参数：$a" >&2; exit 2 ;;
  esac
done

SSH=(ssh -i "$KEY" -o ProxyCommand=none -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 -o ServerAliveInterval=20 "$HOST")
cd "$(dirname "$0")/.."
(python3 ops/scan-secrets.py || python ops/scan-secrets.py) || { echo "密钥扫描未通过，停止部署" >&2; exit 1; }
SHA="$(git rev-parse --short HEAD)"
TS="$(date +%Y%m%d-%H%M%S)"
REL_ID="$SHA-$TS"
REL_DIR="/opt/nonebot-releases/$REL_ID"
if [[ -n "$(git status --porcelain)" ]]; then
  echo "WARN 工作区有未提交改动，部署的是 HEAD（$SHA），不是当前工作区文件" >&2
fi
PREV_REL="$("${SSH[@]}" "cat /opt/nonebot/.release_id 2>/dev/null" | tr -d "[:space:]" || true)"
if [ -z "$PREV_REL" ]; then
  PREV="$("${SSH[@]}" "cat /opt/nonebot/.deployed_sha 2>/dev/null" | tr -d "[:space:]" || true)"
  PREV_REL="legacy-${PREV:-unknown}"
fi
ROLLBACK="ssh $HOST 'sudo -n bash $REL_DIR/ops/release-switch.sh rollback $PREV_REL'"

echo "== 1/7 云端旧版本快照（回滚点）"
"${SSH[@]}" "sudo -n mkdir -p /opt/backup && sudo -n tar czf /opt/backup/code-$TS.tgz -C /opt/nonebot --ignore-failed-read app_core plugins adapters companion_core personas ops bot.py config.example.yaml requirements.txt README.md .env.example .deployed_sha && sudo -n ls -l /opt/backup/code-$TS.tgz"

echo "== 2/7 构建 release：$REL_DIR"
"${SSH[@]}" "sudo -n rm -rf '$REL_DIR' && sudo -n mkdir -p '$REL_DIR/ops'"
git archive HEAD:nonebot | "${SSH[@]}" "sudo -n tar -x -C '$REL_DIR'"
git archive HEAD:ops | "${SSH[@]}" "sudo -n tar -x -C '$REL_DIR/ops'"
"${SSH[@]}" "sudo -n chmod +x '$REL_DIR/ops/'*.sh && sudo -n chown -R ubuntu:ubuntu '$REL_DIR' && sudo -n bash '$REL_DIR/ops/release-switch.sh' prepare '$REL_DIR'"
"${SSH[@]}" "echo '$SHA' | sudo -n tee '$REL_DIR/.deployed_sha' >/dev/null"
"${SSH[@]}" "echo '$REL_ID' | sudo -n tee '$REL_DIR/.release_id' >/dev/null"

if [ "$PRE_VERIFY" = 1 ]; then
  echo "== 3/7 release 内验证（--pre，不碰生产库、不发消息）"
  if ! "${SSH[@]}" "sudo -n '$REL_DIR/ops/verify.sh' --pre --code '$REL_DIR'"; then
    echo >&2
    echo "!! release 验证失败：生产 current / 版本标记都没动，服务仍跑旧版本。" >&2
    echo "!! 现场保留在 $REL_DIR（确认后 sudo rm -rf $REL_DIR）" >&2
    exit 1
  fi
else
  echo "== 3/7 跳过 release 验证（--skip-verify）"
fi

if [ "$RESTART" = 0 ]; then
  echo "== 完成：release 已验证，未切换、未重启（--no-restart）；release：$REL_DIR"
  exit 0
fi

echo "== 4/7 原子切换 current → $REL_ID"
"${SSH[@]}" "sudo -n bash '$REL_DIR/ops/release-switch.sh' activate '$REL_DIR' '$REL_ID'"
"${SSH[@]}" 'echo deployed=$(cat /opt/nonebot/.deployed_sha); systemctl is-active nonebot'

if [ "$POST_VERIFY" = 1 ]; then
  echo "== 5/7 重启后全量验证（五件套）"
  "${SSH[@]}" "sudo -n /opt/nonebot/ops/verify.sh" || {
    echo "!! 全量验证有失败。服务已是新版本；回滚：" >&2
    echo "!! $ROLLBACK" >&2
    exit 1
  }
fi

echo "== 6/7 旧 release 保留在 /opt/nonebot-releases（手工按需清理）"
echo "== 7/7 完成：HEAD $SHA 已部署；回滚目标 $PREV_REL"
echo "== 回滚：$ROLLBACK"
echo "== 快照：/opt/backup/code-$TS.tgz"
