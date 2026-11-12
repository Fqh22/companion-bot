# AGENTS.md

> 给接手这个项目的 AI/人看的入口。**先读这份，再读代码。**
> 项目是一个**聊天内核**：`app_core` 管业务，平台只是适配层（QQ 是其中之一）。

## 1. 先跑什么

```bash
# 本地（Windows 也行）：全量 20 套，约 90 秒
export QQBOT_CODE_DIR=$PWD/nonebot
python ops/lint-plugins.py        # 分层/规范（秒级）
python ops/lint-layers.py         # domain / adapters 依赖方向
python ops/test-nonebot-offline.py
python ops/smoke-all.py
python ops/test-runner.py
python ops/test-commands.py
python ops/link-check.py
python ops/smoke-plugins.py
python ops/test-guards.py         # 护栏自检：lint 规则必须能抓到反例
python ops/replay-diff.py 5       # 全链路冒烟：真实对话重放，只看召回一致/无异常（会调 LLM，不进 CI）
python ops/memory-report.py 7     # 记忆使用周报：0 条率 / 平均条数 / 类型分布（观察期每周跑）

# 云端：五件套
ops/verify.sh --pre              # 重启前：不碰生产库
ops/verify.sh                    # 全量
```

**改完必须全绿**。CI 也会跑，但本地先跑能省一轮。

## 2. 目录与分层

| 路径 | 层 | 说明 |
|---|---|---|
| `nonebot/app_core/` | domain / orchestration / infra | 业务内核，**不许 import nonebot** |
| `nonebot/adapters/` | 适配 | 平台字段 → `InboundMessage`；只走门面 |
| `nonebot/plugins/` | 平台胶水 | NoneBot 的 `on_message/on_command` |
| `nonebot/companion_core/` | 门面包 | `EngineConfig` + `create_engine` |
| `ops/` | 运维/测试 | 每个脚本可独立跑 |
| `docs/` | 文档 | 00 概览、01 系统、02 运维、04 联动、06 移植、07 分层 |

分层规则由 `ops/lint-layers.py` 强制（详见 `docs/06-扩展与移植.md`）：

- **domain**（`prompt` `pacing` `delivery` `intent` `mood_domain` …）只做纯规则，不碰 IO，不 import orchestration；
- **orchestration**（`engine` `runner` `commands` `runtime` `chat` `scheduler` `proactive_runtime` …）负责编排；
- **infra**（`paths` `config` `files` `kernel/*`）只提供基础设施。

## 3. 加东西只走这五条路

| 要加什么 | 改哪里 | 检查 |
|---|---|---|
| **业务模块** | `app_core/<name>.py`，docstring 首行标 `[domain]` 或 `[orchestration]` | `lint-layers` |
| **通道** | 实现 `Channel` 三原语（`adapters/console.py` 是模板），用 `TurnRunner` | `test-runner` |
| **命令** | `app_core/help.py` 登记 + 插件 handler；新通道用 `commands.default_registry` | `lint-plugins`（覆盖检查）+ `test-commands` |
| **LLM 工具** | `TOOLS` 加 schema + `HANDLERS` 加实现（两处名字必须一致，模块加载会自检） | `smoke-all` |
| **定时任务** | `scheduling/scheduler.py` 的 `JOBS` 加一行 + `_run_job` 分支 | `smoke-all` / `smoke-clock` |
| **配置项** | `config.DEFAULTS` + `config.example.yaml` + `ops/config-ui.py` 键表 | `test-config-ui` |

## 4. 红线（lint 会拦，别绕）

1. `app_core` 不许 `import nonebot`——平台调用放 `plugins/`。
2. 运行时路径不许写死 `/opt`——从 `app_core/paths.py` 派生。
3. 插件只能调 `CORE_API` 清单里的 `core` 成员；新成员先登记。
4. 不许 `core.memory.*` / `core._*` 穿透——用门面方法。
5. 不许裸 `print`——统一 logger。
6. domain 不许 import orchestration。
7. adapters 不许 import `chat` / `chat_turn` / `tool_loop`。
8. 新代码的记忆读写只走 `MemoryPort`（`docs/10`），不直接 import `memory` / `profile` / `wiki`。
   已迁：fact 读写、knowledge 读写、profile 读写、forget、trace；mood/task 未进契约，别硬塞。
   机器检查：`lint-plugins.py` 的 `_MEM_ALLOWED` 按「文件 → 模块 → 符号」冻结存量直连；
   新增文件 / 新模块 / 新符号直接 FAIL，迁掉一个就从白名单删一个（没人用的条目会 WARN）。

## 5. 改完要留什么

- **CHANGELOG.md**：按日期加一条，写「改了什么 + 为什么 + 验证数字」。
- **测试**：新行为配一条 `check()`，放在对应的 `ops/test-*.py`；纯逻辑优先。
- **文档**：改了对外行为/命令/配置，同步 `docs/` 对应页。
- **决策**：改了 `docs/00` 里已记录的决策，必须在 CHANGELOG 写「为什么改」，否则会被当成默认值。

## 6. 已定、别随手推翻的决策

| 决策 | 位置 |
|---|---|
| 记忆放历史之后、画像作稳定前缀（缓存） | `docs/00` |
| 回复默认 model-first + 信号包，规则只兜底 | `prompt.py` / `chat.py` |
| 主动消息只在「有 followup 话头」时开口 | `proactive.py` |
| 思考常开（`llm.thinking_casual=false`） | `config.py` |
| 引用回复默认关（`chat.quote_reply=false`） | `config.py` |
| 分层不物理搬家，用 docstring 标记 + lint | `docs/06` |

## 7. 常见错误（AI 最容易犯）

- 在 `plugins/` 里直接 `import app_core.chat`——应该用 `from plugins.chat import core`。
- 新建一个平行模块（`new_tools.py`）而不是注册进 `HANDLERS`。
- 加工具只改 `TOOLS` 不改 `HANDLERS`（加载就抛错）。
- 测完只跑一个脚本就说「全绿」——至少跑第 1 节的 8 条。
- 把测试写死日期（`2026-11-06 09:00`）——必须从 `time.time()` 派生。

## 8. 设计判据（加东西之前）

项目的定义是「记忆体」：对话是端口，记忆是主链路。加任何功能前先回答：

1. **它读什么记忆、写什么记忆？** 答不上来就不加。
2. **写的是「跨会话仍成立的断言」吗？** 不是就只进日志。
3. **会让记忆层变成万能层吗？** 会就拒绝。
4. **会让表达污染事实吗？** 会就拆开。

完整版见 `docs/09-设计初衷.md`。

## 9. 排障入口

| 症状 | 先看 |
|---|---|
| 它不回消息 | `journalctl -u nonebot -n 50`；`/ping` |
| 记忆不写 | `/ping` 的配置状态；`admin.user_id` 是否配 |
| 定时任务没跑 | `job_runs` 表；`/debug`；`missing_jobs` |
| 语气/长度不对 | `data/prompt_log` 的最近一条；`/prompt_last` |
| 通道断了 | `docs/06-扩展与移植.md` |
