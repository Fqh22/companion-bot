# admin

基础命令：存活检查与人格重载。

## 命令

| 命令 | 权限 | 说明 |
|---|---|---|
| `/ping` | 所有人 | 返回 pong |
| `/help` | 所有人 | 命令帮助 |
| `/reload_persona` | 超管 | 重读 `data/persona.json`，不重启进程 |

## 说明

`/reload_persona` 只重载人格，**不重载 `config.yaml`**——配置改完仍要 `systemctl restart nonebot`。
