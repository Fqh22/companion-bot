# agent 插件

把本机的 `agentd`（通用 agent 运行时，127.0.0.1:8100）接到 QQ。

## 用途

| 命令 | 作用 |
|---|---|
| `/task 要办的事` | 建一个任务，agentd 在沙箱里自主执行 |
| `/tasks` | 看最近 8 个任务的状态 |
| `/progress 任务号` | 看某个任务的报告 |
| `/cancel 任务号` | 取消（保留检查点） |
| `/pause 任务号` | 当前步骤跑完后暂停 |
| `/resume 任务号` | 从暂停点继续 |
| `/agent_usage` | 今日 agentd 的 token 分布（`/usage` 是聊天那条，别撞名） |

全部命令 `permission=SUPERUSER`。

## 消息怎么出去

agentd 只写数据库（`outbox` 表），**不直接连 OneBot**。本插件每 2 秒拉一次
`/outbox?since=<游标>`，逐条发私聊，成功后 `/outbox/ack` 推进游标。

这样做的原因：不重复占一条 OneBot 连接，也不用把 token 散到两个进程；
发送失败时不推进游标，下一轮会重试。

## 依赖

- `agentd` 在本机 8100 端口（`systemctl status agentd`）
- 没装或挂了不影响聊天：所有调用都 try/except，失败只记 debug 日志

## 边界

- 这是**通道**，不写业务逻辑。判断、执行、恢复全在 agentd 里。
- 群里不响应（命令带 SUPERUSER 门），避免和私聊人格混淆。
