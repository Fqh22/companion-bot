#!/usr/bin/env bash
# 部署：从 git HEAD 导出到**候选目录**，先验证再切换，最后重启。
#
# 为什么改成候选目录：旧流程先把 tar 解到 /opt/nonebot、写版本号，再跑
# verify --pre。验证失败时生产磁盘已经是新旧混合体，而「没重启」只是
# 让进程还跑着旧代码；下一次重启就会把没验证过的代码带上去。
#
# 现在的顺序：
#   快照（完整发布范围）→ 解到 /opt/nonebot-candidate-<ts> → 在候选目录跑
#   verify --pre → 过了才删除已移除文件、覆盖发布范围、写版本号 → 重启 → 全量验证。
#
# 用法：
#   export QQBOT_HOST=ubuntu@<server>      # 含用户名
#   export QQBOT_KEY=/path/to/key.pem
#   bash ops/deploy.sh                     # 全流程
#   bash ops/deploy.sh --no-restart        # 只建候选并验证，不切换、不重启
#   bash ops/deploy.sh --skip-verify       # 跳过候选验证（仅紧急回滚用）
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

SSH=(ssh -i "$KEY" -o StrictHostKeyChecking=no -o ConnectTimeout=15 -o ServerAliveInterval=20 "$HOST")

cd "$(dirname "$0")/.."
SHA="$(git rev-parse --short HEAD)"
TS="$(date +%Y%m%d-%H%M%S)"
CAND="/opt/nonebot-candidate-$TS"
if [[ -n "$(git status --porcelain)" ]]; then
  echo "WARN 工作区有未提交改动，部署的是 HEAD（$SHA），不是当前工作区文件" >&2
fi
ROLLBACK="ssh $HOST 'sudo -n tar xzf /opt/backup/code-$TS.tgz -C /opt/nonebot && sudo -n find /opt/nonebot -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null; sudo -n systemctl restart nonebot'"

echo "== 1/7 云端旧代码快照（完整发布范围，回滚点）"
# 覆盖范围 = 部署会上传的全部内容：nonebot/ 顶层代码 + ops。不含 venv/data，
# 它们不参与发布，也不该被回滚覆盖。.deployed_sha 一起进快照，回滚时版本
# 标记跟着回去。
"${SSH[@]}" "sudo -n mkdir -p /opt/backup && sudo -n tar czf /opt/backup/code-$TS.tgz -C /opt/nonebot --ignore-failed-read app_core plugins adapters companion_core personas ops bot.py config.example.yaml requirements.txt README.md .deployed_sha && sudo -n ls -l /opt/backup/code-$TS.tgz"

echo "== 2/7 上传 HEAD:$SHA 到候选目录 $CAND"
"${SSH[@]}" "sudo -n rm -rf '$CAND' && sudo -n mkdir -p '$CAND/ops'"
git archive HEAD:nonebot | "${SSH[@]}" "sudo -n tar -x -C '$CAND'"
git archive HEAD:ops | "${SSH[@]}" "sudo -n tar -x -C '$CAND/ops'"
# 候选目录借用生产 venv 跑验证；切换时这个符号链接会被排除，不会覆盖生产 venv。
"${SSH[@]}" "sudo -n ln -sfn /opt/nonebot/venv '$CAND/venv' && sudo -n chmod +x '$CAND/ops/'*.sh && sudo -n chown -R ubuntu:ubuntu '$CAND'"

PREV="$("${SSH[@]}" 'cat /opt/nonebot/.deployed_sha 2>/dev/null' || true)"
PREV="$(printf '%s' "$PREV" | tr -d '[:space:]')"
DELETES=()
if [ -n "$PREV" ] && git cat-file -e "${PREV}^{commit}" 2>/dev/null; then
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    case "$f" in
      nonebot/*) DELETES+=("/opt/nonebot/${f#nonebot/}") ;;
      ops/*)     DELETES+=("/opt/nonebot/ops/${f#ops/}") ;;
    esac
  done < <(git diff --name-only --diff-filter=D "$PREV" "$SHA" -- nonebot ops)
else
  echo "WARN 读不到上次部署 sha（或不是本仓库的 commit），跳过删除清单"
fi
RM_LINES=""
for p in "${DELETES[@]}"; do
  RM_LINES+="rm -f '$p'"$'\n'
done

if [ "$PRE_VERIFY" = 1 ]; then
  echo "== 3/7 候选目录验证（--pre，不碰生产库、不发消息，生产目录未被覆盖）"
  if ! "${SSH[@]}" "sudo -n '$CAND/ops/verify.sh' --pre --code '$CAND'"; then
    echo >&2
    echo "!! 候选目录 verify --pre 失败：**生产目录与版本标记都没动**，服务仍在跑旧代码。" >&2
    echo "!! 排查现场保留在 $CAND（确认后 rm -rf $CAND）" >&2
    exit 1
  fi
else
  echo "== 3/7 跳过候选验证（--skip-verify）"
fi

if [ "$RESTART" = 0 ]; then
  echo "== 完成：候选目录已验证，未切换、未重启（--no-restart）；候选：$CAND"
  exit 0
fi

echo "== 4/7 切换：删除已移除文件，覆盖发布范围，版本标记随切换更新"
"${SSH[@]}" "sudo -n bash -s" <<REMOTE
set -e
$RM_LINES
cd '$CAND'
sudo -n tar --exclude=venv --exclude=__pycache__ --exclude=.deployed_sha -cf - . | sudo -n tar -C /opt/nonebot -xf -
sudo -n chown -R ubuntu:ubuntu /opt/nonebot/app_core /opt/nonebot/adapters /opt/nonebot/plugins /opt/nonebot/ops /opt/nonebot/companion_core /opt/nonebot/personas 2>/dev/null || true
sudo -n find /opt/nonebot -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
sudo -n chmod +x /opt/nonebot/ops/*.sh
printf '%s\n' '$SHA' > /opt/nonebot/.deployed_sha
echo deployed_sha=\$(cat /opt/nonebot/.deployed_sha)
REMOTE

echo "== 5/7 重启并确认存活"
"${SSH[@]}" "sudo -n systemctl restart nonebot; sleep 5; systemctl is-active nonebot"
"${SSH[@]}" 'echo deployed=$(cat /opt/nonebot/.deployed_sha); systemctl is-active nonebot'

if [ "$POST_VERIFY" = 1 ]; then
  echo "== 6/7 重启后全量验证（五件套）"
  "${SSH[@]}" "sudo -n /opt/nonebot/ops/verify.sh" || {
    echo "!! 全量验证有失败。服务已是新代码；先看上面的失败项，必要时回滚。" >&2
    echo "!! 回滚：$ROLLBACK" >&2
    exit 1
  }
fi

echo "== 7/7 清理候选目录"
"${SSH[@]}" "sudo -n rm -rf '$CAND'"
echo "== 完成：HEAD $SHA 已部署；回滚点 /opt/backup/code-$TS.tgz"
echo "== 回滚：$ROLLBACK"
