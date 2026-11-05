#!/bin/sh
# agentd 防护：随系统启动 + 每 5 分钟校准一次。
# 起因：agent 跑吃内存的命令时，内核 OOM 可能挑最大进程杀——那是 QQ 容器，
# 代价是重新扫码登录，远大于任务失败。

# 1) 沙箱子网不许直连宿主（唯一出口是代理容器，不是网关）
iptables -C INPUT -s 172.19.0.0/16 -d 172.19.0.1 -j DROP 2>/dev/null \
  || iptables -I INPUT -s 172.19.0.0/16 -d 172.19.0.1 -j DROP 2>/dev/null

# 2) OOM 优先级：QQ 容器保命，沙箱先死
# docker top 不认 -q，用 awk 取第二列 PID
pids() {
    docker top "$1" 2>/dev/null | awk 'NR>1 {print $2}'
}

for p in $(pids snowluma); do
    echo -500 > "/proc/$p/oom_score_adj" 2>/dev/null
done
for p in $(pids agentd-sandbox); do
    echo 500 > "/proc/$p/oom_score_adj" 2>/dev/null
done
exit 0
