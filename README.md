# companion

> 一个自托管的陪伴引擎：**记得住、有情绪**。
> QQ 只是目前接入的一种通道，不是这个项目的底座。

非腾讯官方项目，与腾讯 / QQ 无任何关联，仅用于个人自建。

## 它是什么

这不是一个「QQ 机器人」，是一个**通道无关的陪伴引擎**：

```
你的平台（QQ / 终端 / Telegram / Web / …）
        │  只做三件事：收消息、发消息、显示「正在输入」
        ▼
   adapters/          平台字段 → InboundMessage
        ▼
   Engine             一问一答的门面
        ▼
   app_core/          记忆、情绪、知识、画像、prompt、调度（不 import 任何平台）
        ▼
   TurnRunner         typing 心跳、流式分段、45 秒硬线、补发
        ▼
   你的平台
```

- **引擎层**（`nonebot/app_core/`）：对话、长期记忆、情绪、知识库、画像、矛盾检测、调度。
  一行 `import nonebot` 都没有，lint 在守。
- **会话循环层**（`app_core/runner.py`）：typing、流式、节奏、超时。与平台无关。
- **适配层**（`nonebot/adapters/` + `plugins/`）：把某个平台的字段翻译成 `InboundMessage`，
  再实现三个发送原语。
- **QQ 适配层**（`adapters/qq.py` + `plugins/chat|group|commands|admin|schedule|proactive`）：
  只是其中一种接法，而且是最容易失效的一种（见下文）。

## 接入一个通道要做什么

只需要三个原语：

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

**这就完了。** 记忆、情绪、prompt、分段、停顿、45 秒硬线全部复用。
完整说明见 [docs/06-移植指南.md](docs/06-移植指南.md)。

## 快速开始（不需要 QQ）

要求 Python **3.12**（CI 只测这个版本；`requirements.txt` 是 3.12 的冻结点）。

```bash
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r nonebot/requirements.txt

export QQBOT_CODE_DIR=$PWD/nonebot                 # Windows: $env:QQBOT_CODE_DIR="$PWD\nonebot"
python ops/lint-plugins.py          # 分层与规范检查（纯 AST，秒级）
python ops/test-nonebot-offline.py  # 离线回归：不联网、模型与 embedding 全 mock
```

看到 `全部通过` 就说明这套代码在你机器上是自洽的。

**想直接和它聊**（仍然不需要 QQ）：`python ops/drill-console.py`，
它会用临时根起一个终端通道，不碰生产库。

## 已提供的适配层

| 适配层 | 文件 | 说明 |
|---|---|---|
| console | `nonebot/adapters/console.py` | 终端；也是新通道的**参考实现** |
| QQ / OneBot v11 | `nonebot/adapters/qq.py` + `nonebot/plugins/` | 通过 SnowLuma / NapCat 等实现接入 |

### 为什么 QQ 只是「演示」

QQ 没有面向个人号的官方机器人 API。现在的接入靠 OneBot v11 实现（SnowLuma / NapCat 等），
它们**逆向 QQ 私有协议或 hook 官方客户端**，不是稳定契约：

- 可能随时断：协议变更、风控、乃至法律原因。2026-11 已有实现收到律师函。
- 有封号风险：代码无法规避，建议用独立小号。
- 换实现的成本很低：OneBot v11 是事实标准，改 `.env` 里的 WS 地址即可。

**所以架构上刻意把 QQ 放在最外层**：它坏了，换掉它，引擎不动。
接 QQ 的具体步骤（OneBot 实现、token、SUPERUSERS）见 [docs/02-运维.md](docs/02-运维.md)。

## 引擎能力

- **聊天**：流式生成，按情绪切 18–32 字的分段，边生成边发；45 秒硬线
- **记忆**：对话 → 总结 → 结构化事实 → 向量召回；被用过就续命，长期不用才归档
- **情绪**：心情 / 基线 / 精力三值，冲量带饱和、衰减可复合；只影响「怎么说」
- **知识库**：模型自己写笔记，带保质期；过期会标「可能过时」
- **画像**：复盘时写「它是怎么理解你的」，带置信度和最后证据时间
- **矛盾检测**：新事实推翻旧事实时软删旧记忆
- **调度**：日记、复盘、归档、清理、周报；带补跑窗口和缺跑审计
- **主动消息**：只在「他留了话头」时开口，宁可不发也不刷屏

## 数据与配置

运行时根默认 `/opt`，用 `QQBOT_ROOT` 覆盖（全部在 `app_core/paths.py` 派生一次）：

| 路径 | 内容 |
|---|---|
| `$QQBOT_ROOT/nonebot/data` | `chat.db`、人格、影子日志、prompt 快照 |
| `$QQBOT_ROOT/qqbot-data` | 记忆库、`keys.json`、学习资料 |
| `$QQBOT_ROOT/backup` | 备份 |

`config.yaml` 管模型、上下文、情绪参数、影子开关；`keys.json`（600）管 API key。
两者都不入库，模板见 `nonebot/config.example.yaml`。

## 几个刻意的取舍

1. **引擎不碰框架**：业务规则全在可测的纯函数里，平台适配只做 IO。
2. **prompt 分稳定前缀与易变段**：人格/画像在前，历史与当前输入在后，为了命中上下文缓存。
3. **情绪是状态不是计数器**：三值 + 惰性衰减，改参数要重标定下游阈值。
4. **一切学习都落盘**：记忆、纠正、画像、技能都是文件和 SQLite，能 diff、能回滚、能被测试扫到。
5. **不追求「更热闹」**：主动消息有闸门，群聊默认关闭，宁可不发也不刷屏。

## 边界

- 单用户、单进程、单机：不是多租户服务
- 群聊默认关闭；开了也只有 @ 才回，且不进记忆库
- 不做多 agent 并行、不做浏览器自动化、不接本地模型
- 不做多步任务/沙箱执行；陪伴聊天是唯一主链路

## 测试

```bash
ops/verify.sh            # 五件套：分层 / 离线 / 联动 / 全链路 / 插件
ops/verify.sh --pre      # 重启前：不碰生产库、不发消息
ops/verify.sh --quick    # 秒级：分层 + 联动
ops/test-runner.py       # 通道无关层：流式 / typing / 超时 / 补发
ops/drill-console.py     # 断线演练：没有 QQ 也能跑一轮真实对话
```

「联动体检」会真把插件跑起来、断言调用次数和参数——单层全绿但接线断了，
是这类项目最常见的故障。哪条链路由哪个测试守着，见 [docs/04-联动矩阵.md](docs/04-联动矩阵.md)。

## 文档

| 想了解 | 读 |
|---|---|
| 消息进来后发生什么、数据在哪 | [docs/01-系统.md](docs/01-系统.md) |
| 部署、备份、排障 | [docs/02-运维.md](docs/02-运维.md) |
| 哪条链路坏了会被哪个测试发现 | [docs/04-联动矩阵.md](docs/04-联动矩阵.md) |
| 换掉 QQ、移植到别的平台 | [docs/06-移植指南.md](docs/06-移植指南.md) |
| 可视化改配置（本地面板） | [docs/05-配置界面.md](docs/05-配置界面.md) |

## License

[MIT](LICENSE)
