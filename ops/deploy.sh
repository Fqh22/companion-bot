#!/usr/bin/env bash
# 部署：从 git HEAD 导出，写入 commit sha，**先验证再重启**。
#
# 为什么不用逐个 scp：代码/脚本漏传一个，就会出现「改了却跑的是旧代码」。
# 2026-11-15 的教训：一次手工 scp 覆盖 ops/ 时丢了脚本可执行位，
# 云端 cron 的备份与健康检查连续一天静默失败（备份缺 11-15 那份）。
# 所以这里显式恢复权限位，并把「重启前验证」做成硬门：验证不过就不重启。
#
# 用法：
#   export QQBOT_HOST=ubuntu@<server>      # 含用户名
#   export QQBOT_KEY=/path/to/key.pem
#   bash ops/deploy.sh                     # 全流程（快照→上传→验证→重启→全量验证）
#   bash ops/deploy.sh --no-restart        # 只上传+验证，不重启（排障）
#   bash ops/deploy.sh --skip-verify       # 跳过重启前验证（不建议；只在紧急回滚时用）
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
    -h|--help) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "未知参数：$a" >&2; exit 2 ;;
  esac
done

SSH=(ssh -i "$KEY" -o StrictHostKeyChecking=no -o ConnectTimeout=15 -o ServerAliveInterval=20 "$HOST")

cd "$(dirname "$0")/.."
SHA="$(git rev-parse --short HEAD)"
TS="$(date +%Y%m%d-%H%M%S)"
if [[ -n "$(git status --porcelain)" ]]; then
  echo "WARN 工作区有未提交改动，部署的是 HEAD（$SHA），不是当前工作区文件" >&2
fi

echo "== 1/6 云端旧代码快照（回滚点）"
"${SSH[@]}" "sudo -n mkdir -p /opt/backup && sudo -n tar czf /opt/backup/code-$TS.tgz -C /opt/nonebot app_core plugins adapters && sudo -n ls -l /opt/backup/code-$TS.tgz"

echo "== 2/6 上传 HEAD:$SHA 的 nonebot/ 与 ops/"
git archive HEAD:nonebot | "${SSH[@]}" "tar -x -C /opt/nonebot"
git archive HEAD:ops | "${SSH[@]}" "tar -x -C /opt/nonebot/ops"
# 云端遗留的同名旧脚本会遮住仓库里的新脚本：verify.sh 曾经优先 offline-test.py，
# 于是「云端 verify 5/5」跑的是旧回归集。仓库名是 test-nonebot-offline.py。
"${SSH[@]}" "rm -f /opt/nonebot/ops/offline-test.py"

echo "== 3/6 权限 / 缓存 / 部署版本号"
"${SSH[@]}" "chown -R ubuntu:ubuntu /opt/nonebot/app_core /opt/nonebot/adapters /opt/nonebot/plugins /opt/nonebot/ops
  find /opt/nonebot -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
  chmod +x /opt/nonebot/ops/*.sh
  printf '%s\n' $SHA > /opt/nonebot/.deployed_sha"
"${SSH[@]}" 'echo deployed_sha=$(cat /opt/nonebot/.deployed_sha)'

if [ "$PRE_VERIFY" = 1 ]; then
  echo "== 4/6 重启前验证（--pre，不碰生产库、不发消息）"
  if ! "${SSH[@]}" "sudo -n /opt/nonebot/ops/verify.sh --pre"; then
    echo >&2
    echo "!! verify --pre 失败：**没有重启**，生产仍在跑旧代码。" >&2
    echo "!! 回滚代码：ssh $HOST 'tar xzf /opt/backup/code-$TS.tgz -C /opt/nonebot && find /opt/nonebot -name __pycache__ -type d -exec rm -rf {} + && sudo systemctl restart nonebot'" >&2
    exit 1
  fi
fi

if [ "$RESTART" = 0 ]; then
  echo "== 完成：已上传并验证，未重启（--no-restart）"
  exit 0
fi

echo "== 5/6 重启并确认存活"
"${SSH[@]}" "sudo -n systemctl restart nonebot; sleep 5; systemctl is-active nonebot"
"${SSH[@]}" 'echo deployed=$(cat /opt/nonebot/.deployed_sha); systemctl is-active nonebot'

if [ "$POST_VERIFY" = 1 ]; then
  echo "== 6/6 重启后全量验证（五件套）"
  "${SSH[@]}" "sudo -n /opt/nonebot/ops/verify.sh" || {
    echo "!! 全量验证有失败。服务已是新代码；先看上面的失败项，必要时用下面的回滚。" >&2
    echo "!! 回滚：ssh $HOST 'tar xzf /opt/backup/code-$TS.tgz -C /opt/nonebot && find /opt/nonebot -name __pycache__ -type d -exec rm -rf {} + && sudo systemctl restart nonebot'" >&2
    exit 1
  }
fi

echo "== 完成：HEAD $SHA 已部署；回滚点 /opt/backup/code-$TS.tgz"
