# companion-bot

> 一个自建的 QQ 陪伴机器人：**记得住、有情绪、能派活**。  
> A self-hosted QQ companion bot with long-term memory, mood, and a sandboxed task agent.

非腾讯官方项目，与腾讯 / QQ 无任何关联，仅用于个人自建。

## 它是什么

同一套代码里跑两条互不干扰的链路：

```
QQ
 ├─ 普通消息 → 陪伴机器人（NoneBot2）  秒回、记得你、有情绪
 └─ /task    → agentd（多步 agent）     沙箱里干活、崩溃可恢复、会问你
```

- **聊天**：私聊为主，按 25 字切段、按情绪调节奏，边生成边发
- **记忆**：对话 → 总结 → 结构化事实 → 向量召回，被用过就续命，长期不用才归档
- **情绪**：心情 / 基线 / 精力三值，冲量带饱和、衰减可复合；只影响「怎么说」
- **知识库**：模型自己写笔记，带保质期，过期会标「可能过时」，周日巡检
- **画像**：复盘时写「它是怎么理解你的」，每条带置信度和最后证据时间，会过期
- **矛盾检测**：新事实推翻旧事实时把旧的软删，避免两条互斥记忆一起进 prompt
- **派活**：`/task` 交给 agentd，多步规划、docker 沙箱、断点恢复、危险命令审批

## 架构

```
QQ ── OneBot v11 ──▶ NoneBot
                       ├─ plugins/   只做 IO（chat / group / agent / commands / admin / schedule / proactive）
                       └─ app_core/  纯逻辑，不 import nonebot（可离线测）
                             │
                             ├─ chat.db        消息 / 用量 / 纪要 / 情绪 / 作业台账
                             └─ livingmemory.db 记忆原子 + 向量
                             ▲
                             │ HTTP（127.0.0.1）
                          agentd（独立进程，docker 沙箱 + 白名单出网）
```

分层红线只有一条：**`app_core` 不 import nonebot**。所以它能在没有框架、没有网络、
没有数据库的情况下被离线测试直接驱动。

## 快速开始（不需要 QQ）

要求 Python 3.10+。

```bash
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r nonebot/requirements.txt

export QQBOT_CODE_DIR=$PWD/nonebot                 # Windows: $env:QQBOT_CODE_DIR="$PWD\nonebot"
python ops/lint-plugins.py          # 分层与规范检查（纯 AST，秒级）
python ops/test-nonebot-offline.py  # 离线回归：不联网、模型与 embedding 全 mock
```

看到 `全部通过` 就说明这套代码在你机器上是自洽的。

## 接到 QQ

1. 装一个 OneBot v11 实现（NapCat / SnowLuma / Lagrange 等），指向本机的反向 WS 端口
2. `cp nonebot/.env.example nonebot/.env`，填 `ONEBOT_ACCESS_TOKEN` 和 `SUPERUSERS`
3. `cp nonebot/config.example.yaml nonebot/config.yaml`，至少改：
   - `admin.user_id` —— 你的 QQ 号。**留空会导致记忆不写入、主动消息不发**（启动日志会警告）
   - `llm.api` / `llm.model`，以及 embedding 的服务地址
4. 把 API key 写到 `/opt/qqbot-data/keys.json`（权限 600，格式见 `app_core/llm.py`）
5. `cd nonebot && python bot.py`（`.env` 必须在 `nonebot/` 下）

更完整的部署、备份、排障见 [docs/02-运维.md](docs/02-运维.md)。

## 目录

```
nonebot/
├─ app_core/     37 个纯逻辑模块（不 import nonebot）
├─ plugins/      7 个插件：chat / group / commands / admin / schedule / proactive / agent
└─ personas/     外人版人格（陌生人私聊用）
agentd/          agent 运行时：代码 / 测试 / 沙箱 / 文档
docs/            系统、运维、agentd、联动矩阵
ops/             验证与运维脚本（部署到 /opt/nonebot/ops）
templates/plugin/ 新插件脚手架（配合 ops/new-plugin.py）
```

## 几个刻意的取舍

1. **app_core 纯逻辑**：插件只做 IO，业务规则全在可测的纯函数里。
2. **prompt 分稳定前缀与易变段**：人格/纪要/记忆/知识目录在前，历史与本轮输入在后，
   为了命中上下文缓存——所以「记忆放历史后面」是有意为之，不是乱排。
3. **45 秒硬线**：超过就补发已生成的部分，不让用户干等；流式优先。
4. **情绪是状态不是计数器**：三值 + 惰性衰减，参数改了要重标定下游阈值——
   文档里专门写了这个坑。
5. **一切学习都落盘**：记忆、纠正、画像、技能都是文件和 SQLite，能 diff、能回滚、
   能被 `ops/coverage-audit.py` 和测试扫到。
6. **不追求「更热闹」**：主动消息有闸门，群聊默认关闭，宁可不发也不刷屏。

## 边界（不做的事）

- 单用户、单进程、单机：不是多租户服务
- 群聊默认关闭；开了也只有 @ 才回，且不进记忆库
- agentd 与聊天**不共享记忆**：它只拿你这一句话和几个文件
- 不做多 agent 并行、不做浏览器自动化、不接本地模型
- 沙箱环境不做复原（装的包重启即失，pip 几秒能装回来）

## 测试

```bash
ops/verify.sh            # 六件套：分层 / 离线 / 联动 / 全链路 / 插件 / agentd
ops/verify.sh --pre      # 重启前：不碰生产库、不发消息
ops/verify.sh --quick    # 秒级：分层 + 联动
```

「联动体检」是真把 nonebot 跑起来、驱动插件 handler，断言**调用次数和参数**——
单层全绿但接线断了，是这类项目最常见的故障。哪条链路由哪个测试守着，见
[docs/04-联动矩阵.md](docs/04-联动矩阵.md)。

## 文档

| 想了解 | 读 |
|---|---|
| 消息进来后发生什么、数据在哪、阈值多少 | [docs/01-系统.md](docs/01-系统.md) |
| 部署、备份、排障、维护 | [docs/02-运维.md](docs/02-运维.md) |
| agentd 怎么干活（多步 / 沙箱 / 恢复 / 审批） | [docs/03-agentd.md](docs/03-agentd.md) |
| 哪条链路坏了会被哪个测试发现 | [docs/04-联动矩阵.md](docs/04-联动矩阵.md) |

## License

[MIT](LICENSE)
