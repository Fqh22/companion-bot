#!/usr/bin/env bash
# 部署：从 git HEAD 导出，写入 commit sha，重启并校验。
#
# 为什么不用逐个 scp：代码存在于三处（本地仓库 / /opt 部署 / /tmp 临时 clone），
# 逐个 scp 漏一个就会出现「改了却跑的是旧代码」——2026-11-15 因此浪费了三次排障。
# git archive 保证「部署的就是 HEAD 这一版」，sha 落在 .deployed_sha 里可对账。
set -euo pipefail
HOST="${QQBOT_HOST:-ubuntu@150.158.90.134}"
KEY="${QQBOT_KEY:?请先 export QQBOT_KEY 指向 ssh 私钥}"
cd "$(dirname "$0")/.."
SHA="$(git rev-parse --short HEAD)"
if [[ -n "$(git status --porcelain)" ]]; then
  echo "WARN 工作区有未提交改动，部署的是 HEAD 而不是当前文件" >&2
fi

git archive HEAD:nonebot | ssh -i "$KEY" -o StrictHostKeyChecking=no "$HOST" \
  "tar -x -C /opt/nonebot && echo $SHA > /opt/nonebot/.deployed_sha"
git archive HEAD:ops | ssh -i "$KEY" -o StrictHostKeyChecking=no "$HOST" \
  "tar -x -C /opt/nonebot/ops"

ssh -i "$KEY" -o StrictHostKeyChecking=no "$HOST" \
  "echo deployed=\$(cat /opt/nonebot/.deployed_sha); sudo systemctl restart nonebot; sleep 5; systemctl is-active nonebot"
