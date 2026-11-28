# AGENTS.md

> 给接手这个项目的 AI/人看的入口。**先读这份，再读代码。**
> 项目是一个**可携带、可授权的持续陪伴主体**（现行定义见 `docs/01-定义与决策.md`）：判断/克制第一，记忆是资源，平台只是适配层
> （QQ 与飞书是当前两个通道，终端用于调试）。

## 跨平台接手

- 开工前核对当前 worktree 的分支、提交和未提交改动，再读共享交接卡（`HANDOFF.md`，若存在）。独立 worktree 中若没有这张卡，从共享工作区的项目主目录读取，并核对卡中的分支和提交。
- 在项目根运行 `python ops/lint-plugins.py` 做快速离线检查。下方列出了按改动范围执行的其他验证。
- 默认串行接手项目主工作树，保留已有未提交改动。确有并行任务时再使用独立 Git worktree。部署、恢复、云端备份和真实数据验证按本项目的运维流程执行。

## 1. 验证

在项目根运行 `python ops/lint-plugins.py`。本地验证按[验证清单](docs/README.md#验证清单)执行。代码改动至少跑基础八项和受影响链路的测试，交付前跑完整检查。云端使用 `ops/verify.sh --pre` 和 `ops/verify.sh`。

## 2. 定位代码与审查

改动审查先看 `git status --short` 和 `git diff --stat HEAD`，再读目标差异、直接调用链和对应断言。未跟踪文件从状态清单按需打开。架构审查按链路查阅[文档地图](docs/README.md#按任务阅读)。长测试、基线和变更记录按关键词定位，测试输出保留汇总、失败和相关警告。

`app_core` 放内核，`adapters` 转换平台字段，`plugins` 接平台 IO，`companion_core` 提供应用门面，`ops` 放验证和运维入口。分层与扩展方式见[系统与扩展](docs/02-系统与扩展.md)。

## 3. 扩展入口

- 模块声明 `[domain]`、`[orchestration]` 或 `[infra]`。domain 只做纯规则，orchestration 编排流程，infra 提供基础设施。
- 通道使用 `Channel` 和 `TurnRunner`。命令登记在 `app_core/help.py`，跨端走 `commands.default_registry`。
- LLM 工具登记 `TOOLS`、`HANDLERS` 和 `tool_policy.BUILTIN_POLICIES`。定时任务更新 `scheduling/scheduler.py` 的 `JOBS` 和 `_run_job`。
- 配置同步 `config.DEFAULTS` 与 `config.example.yaml`。

## 4. 红线（lint 会拦，别绕）

1. `app_core` 不许 `import nonebot`——平台调用放 `plugins/`。
2. 运行时路径不许写死 `/opt`——从 `app_core/paths.py` 派生。
3. 插件只能调 `CORE_API` 清单里的 `core` 成员；新成员先登记。插件也不得直接 import 业务实现
   （`lint-layers` 的 `check_plugins`；存量按文件冻结在 `PLUGIN_LEGACY`，2026-12-31 到期）。
4. 不许 `core.memory.*` / `core._*` 穿透——用门面方法。
5. 不许裸 `print`——统一 logger。
6. domain 不许 import orchestration。
7. adapters 不许 import `chat` / `chat_turn` / `tool_loop`。
8. 新代码的记忆读写只走 `MemoryPort`（`docs/01-定义与决策.md`），不直接 import `memory` / `profile` / `wiki`。
   已迁：fact / knowledge / profile / task 读写、forget、trace；mood 走 AffectPort（见 `docs/01-定义与决策.md` §9）。
   机器检查：`lint-plugins.py` 的 `_MEM_ALLOWED` 按「文件 → 模块 → 符号」冻结存量直连；
   新增文件 / 新模块 / 新符号直接 FAIL，迁掉一个就从白名单删一个（没人用的条目会 WARN）。

## 5. 改完要留什么

- **CHANGELOG.md**：按日期加一条，写「改了什么 + 为什么 + 验证数字」。
- **测试**：新行为配一条 `check()`，放在对应的 `ops/test-*.py`；纯逻辑优先。
- **文档**：改了对外行为/命令/配置，同步 `docs/` 对应页。
- **决策**：改了 `docs/01-定义与决策.md` 里已记录的决策，必须在 CHANGELOG 写「为什么改」，否则会被当成默认值。
- **交接看板（`HANDOFF.md`）**：凡改动代码、配置、文档或云端状态，收尾时通过本地 `workspace-memory` MCP 服务的 `checkpoint_put` 更新结构化状态（自动同步 `HANDOFF.md`）；未接入 MCP 时起草摘要经用户确认后更新 `HANDOFF.md`。

文档规则的**唯一真相源**是 `docs/README.md` 的「约定 / 规则」两节（含机器检查项）；
本文件只列必须留痕的动作，不复制规则正文。

## 6. 已定、别随手推翻的决策

| 决策 | 位置 |
|---|---|
| 记忆放历史之后、画像作稳定前缀（缓存） | `docs/01-定义与决策.md` |
| 回复默认 model-first + 信号包，规则只兜底 | `prompt.py` / `chat.py` |
| 主动消息只在「有 followup 话头」时开口 | `proactive.py` |
| 思考常开（`llm.thinking_casual=false`） | `config.py` |
| 引用回复默认关（`chat.quote_reply=false`） | `config.py` |
| 分层不物理搬家，用 docstring 标记 + lint | `docs/02-系统与扩展.md` |

## 7. 测试约定

时间相关测试以 `time.time()` 为基准构造数据。

## 8. 新功能设计

按[定义与决策](docs/01-定义与决策.md)的五问检查连续性、外部能力复用、迁移、审计回滚和事实准确性。

## 9. 排障入口

| 症状 | 先看 |
|---|---|
| 它不回消息 | `journalctl -u nonebot -n 50`；`/ping` |
| 记忆不写 | `/ping` 的配置状态；`admin.user_id` 是否配 |
| 定时任务没跑 | `job_runs` 表；`/debug`；`missing_jobs` |
| 语气/长度不对 | `data/prompt_log` 的最近一条；`/prompt_last` |
| 通道断了 | `docs/02-系统与扩展.md` |
