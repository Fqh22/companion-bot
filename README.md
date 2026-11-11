# 聊天内核 · companion

> 一个自托管的聊天内核：记得住、有情绪。
> 平台只是接入层——QQ 是其中之一，不是项目的底座。

非腾讯官方项目，与腾讯 / QQ 无任何关联，仅用于个人自建。

## 是什么

这不是某个平台的机器人，而是一个**内核 + 适配层**：

```
平台（QQ / 终端 / Telegram / Web / …）
   │  三个原语：收消息、发消息、显示「正在输入」
   ▼
adapters/          平台字段 → InboundMessage
   ▼
Engine             一问一答的门面
   ▼
app_core/          记忆、情绪、知识、画像、prompt、调度
   ▼
TurnRunner         typing 心跳、流式分段、45 秒硬线
   ▼
平台
```

三句话概括设计：

1. **内核不懂平台**——`app_core` 没有一行 `import nonebot`，由 lint 守着。
2. **通道只做三个原语**——发文本、发图、typing；会话循环由内核统一。
3. **换通道不改业务**——记忆、情绪、prompt、分段、命令、调度全部复用。

## 分层

内核内部按职责分三层，规则由 `ops/lint-layers.py` 强制（详见 [docs/07-分层.md](docs/07-分层.md)）：

| 层 | 职责 | 例子 |
|---|---|---|
| **domain** | 纯规则，不碰 IO | `prompt` `pacing` `delivery` `intent` `mood_domain` |
| **orchestration** | 编排一轮对话 / 命令 / 调度 | `engine` `runner` `commands` `runtime` `chat` `scheduler` |
| **infra** | 路径、配置、文件、时钟 | `paths` `config` `files` `kernel/*` |

## 接入一个通道

实现三个原语：

```python
class Channel:
    async def send_text(self, session: str, text: str) -> bool: ...
    async def send_image(self, session: str, path: str) -> bool: ...
    async def typing(self, session: str, on: bool) -> None: ...
```

接线：

```python
from app_core.engine import Engine, InboundMessage
from app_core.runner import TurnRunner

async def on_message(platform_msg):
    msg = InboundMessage(
        session=f"myplat:{platform_msg.user_id}",   # 会话 id，决定记忆归属
        text=platform_msg.text,
        images=platform_msg.images,
        platform="myplat",
    )
    await TurnRunner(engine, MyChannel(), deadline=45).run(msg)
```

命令也有一份现成的注册表：

```python
from app_core.commands import CommandContext, default_registry

registry = default_registry(engine.core)
reply = await registry.dispatch("/mood", CommandContext(session=my_session, is_owner=True))
```

**这就完了。** 记忆、情绪、prompt、分段、停顿、45 秒硬线、只读命令全部复用。
完整说明见 [docs/06-移植指南.md](docs/06-移植指南.md)。

## 快速开始（不需要任何平台）

要求 Python **3.12**（CI 只测这个版本；`requirements.txt` 是 3.12 的冻结点）。

```bash
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r nonebot/requirements.txt

export QQBOT_CODE_DIR=$PWD/nonebot                 # Windows: $env:QQBOT_CODE_DIR="$PWD\nonebot"
python ops/lint-plugins.py          # 分层与规范检查（纯 AST，秒级）
python ops/test-nonebot-offline.py  # 离线回归：不联网、模型与 embedding 全 mock
```

想直接和它聊：`python ops/drill-console.py`——终端通道，用临时根，不碰生产库。

## 已提供的适配层

| 适配层 | 文件 | 说明 |
|---|---|---|
| console | `nonebot/adapters/console.py` | 终端；也是新通道的**参考实现** |
| QQ / OneBot v11 | `nonebot/adapters/qq.py` + `nonebot/plugins/` | 通过 SnowLuma / NapCat 等接入 |

### 为什么 QQ 只是「演示」

QQ 没有面向个人号的官方 API。接入靠 OneBot v11 实现，它们**逆向私有协议或 hook 客户端**，
不是稳定契约：协议变更、风控、乃至法律压力都可能让它随时失效（2026-11 已有实现收到律师函），
且存在封号风险。

**所以 QQ 被放在最外层**：它坏了，换掉它，内核不动。
接入 QQ 的具体步骤见 [docs/02-运维.md](docs/02-运维.md)。

## 能力

- **聊天**：流式生成，按情绪切 18–32 字分段，边生成边发，45 秒硬线
- **记忆**：对话 → 总结 → 结构化事实 → 向量召回；用过就续命，长期不用才归档
- **情绪**：心情 / 基线 / 精力三值，冲量带饱和、衰减可复合；只影响「怎么说」
- **知识库**：模型自己写笔记，带保质期，过期标注「可能过时」
- **画像**：复盘时写「它是怎么理解你的」，带置信度与最后证据时间
- **矛盾检测**：新事实推翻旧事实时软删旧记忆
- **调度**：日记、复盘、归档、清理、周报；带补跑窗口和缺跑审计
- **主动消息**：只在「他留了话头」时开口，宁可不发也不刷屏

## 数据

根目录默认 `/opt`，用 `QQBOT_ROOT` 覆盖（全部在 `app_core/paths.py` 派生一次）：

| 路径 | 内容 |
|---|---|
| `$QQBOT_ROOT/nonebot/data` | `chat.db`、人格、影子日志、prompt 快照 |
| `$QQBOT_ROOT/qqbot-data` | 记忆库、`keys.json`、学习资料 |
| `$QQBOT_ROOT/backup` | 备份 |

`config.yaml` 与 `keys.json` 不入库；模板见 `nonebot/config.example.yaml`。

## 测试

```bash
ops/verify.sh            # 五件套：分层 / 离线 / 联动 / 全链路 / 插件
ops/verify.sh --pre      # 重启前：不碰生产库、不发消息
ops/test-runner.py       # 通道无关层：流式 / typing / 超时 / 补发
ops/test-commands.py     # 命令解析 / 权限 / 默认注册表
ops/drill-console.py     # 断线演练：没有 QQ 也能跑一轮真实对话
```

## 文档

| 想了解 | 读 |
|---|---|
| 内核怎么分层、新模块放哪 | [docs/07-分层.md](docs/07-分层.md) |
| 加模块 / 通道 / 命令 / 工具 / 任务 | [docs/08-扩展指南.md](docs/08-扩展指南.md) |
| 消息进来后发生什么、数据在哪 | [docs/01-系统.md](docs/01-系统.md) |
| 部署、备份、排障 | [docs/02-运维.md](docs/02-运维.md) |
| 哪条链路坏了会被哪个测试发现 | [docs/04-联动矩阵.md](docs/04-联动矩阵.md) |
| 换掉 QQ、移植到别的平台 | [docs/06-移植指南.md](docs/06-移植指南.md) |
| 可视化改配置 | [docs/05-配置界面.md](docs/05-配置界面.md) |

## License

[MIT](LICENSE)
