<div align="center">

# 贡献指南

</div>

感谢你愿意参与。这个项目偏"工程协议"而不是"功能堆叠"：**先约定，再写代码**。

## 0. 先读这三份

1. [AGENTS.md](AGENTS.md) —— 分层红线、五条扩展路径、提交前检查
2. [docs/01-定义与决策.md](docs/01-定义与决策.md) —— 现行定义与已定取舍，别随手推翻
3. [docs/02-系统与扩展.md](docs/02-系统与扩展.md) —— 三层架构与通道抽象

文档只有 5 篇，完整地图见 [docs/README.md](docs/README.md)。

## 1. 环境

Python **3.12**。CI 只测这一版。

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r nonebot/requirements.txt
export QQBOT_CODE_DIR=$PWD/nonebot
```

## 2. 开发循环

```bash
# 秒级：分层与规范
python ops/lint-plugins.py
python ops/lint-layers.py

# 改行为：先跑相关单测，再跑全量
python ops/test-nonebot-offline.py     # 离线回归（不联网、不碰生产库）
python ops/smoke-all.py                # 全链路
python ops/link-check.py               # 联动体检
python ops/check-links.py               # 文档相对链接（改了文档/移动文件后必跑）

# 提交前（等价于 CI 的 18 套）
ops/verify.sh --pre
```

没有"应该没问题"：**改完必须全绿**。CI 会跑同一套，本地先跑能省一轮。

## 3. 代码红线（lint 会拦，别绕）

1. `app_core` 不许 import 任何平台框架（nonebot / lark 等）。
2. 运行时路径不许写死 `/opt`——从 `app_core/paths.py` 派生。
3. 插件只能调 `CORE_API` 白名单里的 `core` 成员；新成员先登记到 `ops/lint-plugins.py`。
4. 不许 `core.memory.*` / `core._*` 穿透——用门面方法。
5. 不许裸 `print`——统一 logger。
6. domain 不许 import orchestration。
7. adapters 不许 import `chat` / `chat_turn` / `tool_loop`。
8. 新代码的记忆读写只走 `MemoryPort`（docs/01-定义与决策.md）；不要直接 import `memory` / `profile` / `wiki`。

## 4. 五条扩展路径

| 要加什么 | 改哪里 | 检查 |
|---|---|---|
| 业务模块 | `app_core/<name>.py`，docstring 标 `[domain]` / `[orchestration]` | lint-layers |
| 通道 | 实现 `Channel`（能力声明 + `send_part` + `typing`），用 `TurnRunner` | test-runner + 该通道自己的离线测试 |
| 命令 | `app_core/help.py` 登记 + 插件 handler；新通道用 `commands.default_registry` | lint-plugins + test-commands |
| LLM 工具 | `TOOLS` 加 schema + `HANDLERS` 加实现（名字必须一致） | smoke-all |
| 定时任务 | `scheduling/scheduler.py` 的 `JOBS` + `_run_job` | smoke-all / smoke-clock |
| 配置项 | `config.DEFAULTS` + `config.example.yaml` + `ops/config-ui.py` 键表 | test-config-ui |

## 5. 提交与 PR

- 提交信息用 Conventional Commits 风格：`feat(scope): ...` / `fix(scope): ...` /
  `docs:` / `refactor:` / `test:`，正文写**为什么**和**验证数字**。
- 一个 PR 做一件事；不要顺手重排无关文件。
- 必须满足：
  - [ ] CI 绿（lint + test 全部 job）
  - [ ] 新行为有对应 `check()`
  - [ ] 对外行为/命令/配置变化同步 `docs/`
  - [ ] `CHANGELOG.md` 加一条（改了什么 + 为什么 + 验证数字）
- 涉及密钥/协议/权限的改动，先看 [SECURITY.md](SECURITY.md)。

## 6. 不要做的事

- 不要为了"跑通"绕过 lint、跳过 `test-guards`、或在测试里写死日期。
- 不要提交 `config.yaml`、`keys.json`、`.env`、备份包、日志。
- 不要直接改生产库或在 CI 里联网调真实模型。
- 不要给个人微信写 hook/协议接入（违规 + 封号，项目明确不支持）。
- 不要私自改已定决策（`docs/01-定义与决策.md`）；确实要改，先开 issue 说明理由与代价。

## 7. 行为准则

对事不对人；技术分歧拿数据和测试说话；不欢迎人身攻击、骚扰与歧视性言论。
维护者有权关闭不符合上述约定的 PR。

## 8. 维护者：部署

部署脚本与备份流程见 [docs/03-运维与通道.md](docs/03-运维与通道.md)。简版：

```bash
ops/verify.sh --pre        # 本地/云端验证
bash ops/deploy.sh         # 快照 → 上传 → 验证 → 重启 → 全量验证
```
