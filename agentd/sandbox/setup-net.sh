#!/bin/sh
# 建沙箱出网环境：内网 + 白名单代理。可重复执行（幂等）。
set -e
NET=agentd-net
PROXY=agentd-proxy

docker network inspect "$NET" >/dev/null 2>&1 || docker network create --internal "$NET"

if docker inspect "$PROXY" >/dev/null 2>&1; then
    docker rm -f "$PROXY" >/dev/null
fi

docker run -d --name "$PROXY" --restart unless-stopped \
    --memory 128m --pids-limit 64 \
    -v /opt/agentd/agentd/sandbox/proxy.py:/proxy.py:ro \
    --entrypoint python3 python:3.12-slim /proxy.py

sleep 2
docker network connect "$NET" "$PROXY"
echo "代理已就绪：$PROXY:8888（白名单见 proxy.py 顶部的 ALLOW）"

# 沙箱必须在代理之后重建，才能拿到新的网络和代理环境变量
docker rm -f agentd-sandbox >/dev/null 2>&1 || true
echo "沙箱已删除，下次执行任务时按需重建"
