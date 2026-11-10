# companion-bot

> 一个自建的 QQ 陪伴机器人：**记得住、有情绪**。  
> A self-hosted QQ companion bot with long-term memory and mood.

非腾讯官方项目，与腾讯 / QQ 无任何关联，仅用于个人自建。

> 项目概览：[docs/00-项目概览.md](docs/00-项目概览.md)。

[English](README.en.md)

## 它是什么

![对话示意](docs/assets/demo.svg)

一条主链路：

```
QQ → OneBot v11 → NoneBot2（adapters / Engine / ChatCore）  秒回、记得你、有情绪
```

- **聊天**：私聊为主，按情绪 18–32 字切段（DeliveryPolicy 可覆盖），边生成边发
- **记忆**：对话 → 总结 → 结构化事实 → 向量召回，被用过就续命，长期不用才归档
- **情绪**：心情 / 基线 / 精力三值，冲量带饱和、衰减可复合；只影响「怎么说」
- **知识库**：模型自己写笔记，带保质期，过期会标「可能过时」，周日巡检
- **画像**：复盘时写「它是怎么理解你的」，每条带置信度和最后证据时间，会过期
- **矛盾检测**：新事实推翻旧事实时把旧的软删，避免两条互斥记忆一起进 prompt

## 架构

![架构](docs/assets/architecture.svg)

分层红线只有一条：**`app_core` 不 import nonebot**。QQ/console 通过 `adapters` 接入，
`Engine` 收 `InboundMessage`，所以核心能在没有框架、没有网络、没有数据库的情况下被离线测试驱动。

## 可移植性：QQ 只是一个通道

核心逻辑全在 `app_core`，**没有一行 `import nonebot`**（lint 在守）。QQ 只是第一个接进来的通道：

- 一个通道只需要实现三个原语：**发文本 / 发图 / 「正在输入」**；
- 会话循环（流式分段、段间停顿、45 秒硬线、typing 心跳）由 `app_core/runner.py` 负责，与平台无关；
- 现在有两个实现：`plugins/chat` 的 `QQChannel` 和 `adapters/console.py`（终端）；
- 换到 NoneBot 的其他平台约 3–5 天，完全不用 NoneBot 约 1–2 周，**业务代码一行不改**。

完整步骤、能力清单和数据迁移见 [docs/06-移植指南.md](docs/06-移植指南.md)。

## 快速开始（不需要 QQ）

要求 Python **3.12**（CI 只测这个版本；`requirements.txt` 是 3.12 的冻结点，3.10/3.11 未验证）。

```bash
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r nonebot/requirements.txt

export QQBOT_CODE_DIR=$PWD/nonebot                 # Windows: $env:QQBOT_CODE_DIR="$PWD\nonebot"
python ops/lint-plugins.py          # 分层与规范检查（纯 AST，秒级）
python ops/test-nonebot-offline.py  # 离线回归：不联网、模型与 embedding 全 mock
```

看到 `全部通过` 就说明这套代码在你机器上是自洽的。


共 **647 条**。少数用例依赖生产库副本（documents / 向量），副本为空时会打印
**SKIP** 而不是失败；有真实数据时全部真跑。

## 路径约定

默认根目录是 `/opt`，可以用 `QQBOT_ROOT` 改：

```bash
export QQBOT_ROOT=/srv/qqbot      # 默认 /opt
```

派生出来的位置（全部在 `nonebot/app_core/paths.py` 算一次，其余模块从它取）：

| 路径 | 内容 |
|---|---|
| `$QQBOT_ROOT/nonebot/data` | `chat.db`、人格、影子日志、prompt 快照 |
| `$QQBOT_ROOT/qqbot-data` | 记忆库、`keys.json`、学习资料（`workspace/learn`） |
| `$QQBOT_ROOT/backup` | 备份 |

离线测试不受影响：它会把落盘路径全部重定向到临时目录。
## 接到 QQ

1. 装一个 OneBot v11 实现（NapCat / SnowLuma / Lagrange 等），指向本机的反向 WS 端口
2. `cp nonebot/.env.example nonebot/.env`，填 `ONEBOT_ACCESS_TOKEN` 和 `SUPERUSERS`
3. `cp nonebot/config.example.yaml nonebot/config.yaml`，至少改：
   - `admin.user_id` —— 你的 QQ 号；多入口可再加 `admin.identities`。**两套都留空会导致记忆不写入、主动消息不发**（启动日志会警告）
   - `llm.api` / `llm.model`，以及 embedding 的服务地址
4. 把 API key 写到 `/opt/qqbot-data/keys.json`（权限 600，格式见 `app_core/llm.py`）
5. `cd nonebot && python bot.py`（`.env` 必须在 `nonebot/` 下）

更完整的部署、备份、排障见 [docs/02-运维.md](docs/02-运维.md)。

## 目录

```
nonebot/
├─ app_core/     纯逻辑模块（不 import nonebot，约 53 个；runner.py 是通道无关循环）
├─ adapters/     qq / console / fake 通道适配器
├─ companion_core/ 稳定入口（EngineConfig + create_engine）
├─ plugins/      6 个插件：chat / group / commands / admin / schedule / proactive
└─ personas/     外人版人格（陌生人私聊用）
docs/            系统、运维、联动矩阵
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
7. **引擎优先**：默认 `style_mode=model` + `context_format=compact`；旧规则保留为 `rules` 回退，
   QQ/console 共用同一个 `Engine`。

## 边界（不做的事）

- 单用户、单进程、单机：不是多租户服务
- 群聊默认关闭；开了也只有 @ 才回，且不进记忆库
- 不做多 agent 并行、不做浏览器自动化、不接本地模型
- 不做多步任务/沙箱执行；陪伴聊天是唯一主链路

## 外部依赖与风险

QQ 没有面向个人号的官方机器人 API。接入靠的是 OneBot v11 实现（NapCat / SnowLuma / Lagrange 等），
它们**逆向 QQ 私有协议、或 hook 官方客户端**，不是稳定契约。

- **可能随时断**：腾讯改协议或加风控，整条链路会失效。历史上 go-cqhttp 曾是事实标准，现在基本停更，
  社区迁到了 NapCat——**主流实现是会换名字的**。
- **换实现的成本很低**：OneBot v11 是事实标准，换一个实现只需要改 `nonebot/.env` 里的 WS 地址和 token，
  业务代码（`app_core` / `plugins`）不用动。
- **有封号风险**：这是 ToS 层面的事，代码无法规避。**建议用独立小号跑，不要接主号**。
- **掉线能被发现**：健康检查每 2 分钟查一次容器/端口，持续失败写日志/告警。

如果你不能接受这些，这个项目不适合你的场景——这不是靠配置能解决的问题。
## 测试

```bash
ops/verify.sh            # 五件套：分层 / 离线 / 联动 / 全链路 / 插件
ops/verify.sh --pre      # 重启前：不碰生产库、不发消息
ops/verify.sh --quick    # 秒级：分层 + 联动
ops/smoke-clock.py       # 真实时钟冒烟：不 mock 时间，跑一遍时间敏感链路
```

「联动体检」是真把 nonebot 跑起来、驱动插件 handler，断言**调用次数和参数**——
单层全绿但接线断了，是这类项目最常见的故障。哪条链路由哪个测试守着，见
[docs/04-联动矩阵.md](docs/04-联动矩阵.md)。

## 文档

| 想了解 | 读 |
|---|---|
| 消息进来后发生什么、数据在哪、阈值多少 | [docs/01-系统.md](docs/01-系统.md) |
| 部署、备份、排障、维护 | [docs/02-运维.md](docs/02-运维.md) |
| 哪条链路坏了会被哪个测试发现 | [docs/04-联动矩阵.md](docs/04-联动矩阵.md) |
| 可视化改配置（本地面板） | [docs/05-配置界面.md](docs/05-配置界面.md) |
| 换掉 QQ、移植到别的平台 | [docs/06-移植指南.md](docs/06-移植指南.md) |

## License

[MIT](LICENSE)
