# 文档地图

> 2026-11-16 重写：原先 19 篇编号文档合并为 **5 篇，按层叙述**。
> 旧编号 00/01/…/22 已删除，历史内容可在 git 历史找回；CHANGELOG 里的旧路径指归档前文件。

## 分层

| 层 | 文档 | 回答什么问题 | 谁先读 |
|---|---|---|---|
| **L1** | [01-定义与决策](01-定义与决策.md) | 它是什么、边界在哪、五条判据、已定取舍 | 所有人 |
| **L2** | [02-系统与扩展](02-系统与扩展.md) | 消息怎么走、三层与红线、五条扩展路径、测试 | 改代码 |
| **L3** | [03-运维与通道](03-运维与通道.md) | 部署、验证、备份恢复、排障、通道接入 | 部署 / 运维 |
| **L4** | [04-评测与实验](04-评测与实验.md) | 怎么知道改好了、影子实验与未落地设计 | 评测 / 调参 |

根目录：`README.md` 对外入口 · `AGENTS.md` 给 AI/开发者的速查与红线 ·
`CHANGELOG.md` 按日期流水 · `CONTRIBUTING.md` 提交约定 · `SECURITY.md` 密钥与平台风险。
`relation.example.md` 是关系小抄示例，不是叙述文档。

## 按任务阅读

| 任务 | 从哪里开始 |
|---|---|
| 启动和排障 | [快速开始](../README.md#快速开始)与[运维与通道](03-运维与通道.md) |
| 审查工作树改动 | [项目约定](../AGENTS.md)、交接看板、Git 状态和目标 diff |
| 审查入站与回复 | [一轮对话](02-系统与扩展.md#2--一轮对话)与[通道协议](02-系统与扩展.md#3--通道协议) |
| 审查记忆与主动跟进 | [系统与扩展](02-系统与扩展.md)第 4、5 节 |
| 审查搜索与工具 | [系统与扩展](02-系统与扩展.md)第 2、7 节，再定位相关工具与测试 |
| 查设计依据或评测结果 | [定义与决策](01-定义与决策.md)、[评测与实验](04-评测与实验.md)及对应的 [变更记录](../CHANGELOG.md)条目 |

## 验证清单

在项目根将 `QQBOT_CODE_DIR` 指向当前工作树的 `nonebot` 目录。PowerShell 可用 `$env:QQBOT_CODE_DIR = (Resolve-Path nonebot).Path`。

| 范围 | 在项目根用 Python 运行的脚本 |
|---|---|
| 基础八项 | `ops/lint-plugins.py`、`ops/lint-layers.py`、`ops/test-nonebot-offline.py`、`ops/smoke-all.py`、`ops/test-runner.py`、`ops/test-commands.py`、`ops/link-check.py`、`ops/smoke-plugins.py` |
| 静态检查与守门员 | `ops/scan-secrets.py`、`ops/check-links.py`、`ops/test-guards.py`、`ops/test-lint-layers.py`、`ops/test-check-links.py`、`ops/test-embed-guard.py` |
| 配置、记忆端口和审批护栏 | `ops/test-batch2-guards.py`、`ops/test-batch3-guards.py`、`ops/test-batch4-guards.py` |
| 独立的线上观测 | `ops/replay-diff.py 5` 用真实对话样本调用模型，独立于 CI。观察期用 `ops/memory-report.py 7` 汇总记忆使用情况 |

完整发布验证使用 `python ops/verify.py --profile all`，只改接线可用 `--profile quick`。
离线回归入口默认跑全套。可用 `--group adapters` 或 `--group pacing` 独立验证，也可用 `--group conversation` 验证到对话组。分组源码在 `ops/offline_regression/`。

用测试汇总和失败附近的输出核对行为，用脚本名和断言文字定位细节。云端部署验证及各链路的测试入口见[测试与联动](02-系统与扩展.md#8--测试与联动)。

## 约定

- 中文写作：先结论后理由；命令贴实际值；数字标日期；引用代码写路径与行号，不写「在 chat 里」。
- 状态标注：稳定 / 设计稿 / 进行中。设计稿不能当作已实现来引用。
- 新增文档：先在这张表登记再写；改文件名要全局搜索替换并跑 `ops/check-links.py`。
- 改对外行为 → 同步对应层文档 + CHANGELOG；改 01 里已记录的决策 → 必须写明「为什么改」。
- **不要再加页**：新内容并入现有层的对应小节，只有全新读者路径才允许新建一层。

## 规则（唯一真相源）

下面四条由 **`ops/check-links.py`** 机器检查，CI 的 lint job 每次推送执行。
`AGENTS.md` 与 `CONTRIBUTING.md` 只放速查与指针，**不再复制规则正文**——
规则散在四处正是上一轮重构后十几处路径引用腐烂的原因。

| 规则 | 检查方式 |
|---|---|
| markdown 相对链接 / 图片目标存在 | 扫描所有 md（锚点只校验文件存在） |
| 正文、反引号、代码与配置注释里的 `docs/NN...` 引用必须存在（**含裸编号**） | 扫描 md / py / yaml / sh |
| 本页登记项 ↔ `docs/` 实际编号文档一一对应 | 新建或删除页必须同步这张表 |
| 守门员自己不能永远绿 | `ops/test-check-links.py` 在临时目录故意写坏 6 类文档 |

范围：扫描**工作区**而非 `git ls-files`，新建但还没 `git add` 的文档同样被检查。
排除：`CHANGELOG.md` 是历史流水，引用归档前路径是合理的；
护栏脚本自身含反例，也不参与扫描。

**脚本分工**：`ops/link-check.py` 只管插件 ↔ 内核的代码接线；
`ops/check-links.py` 只管文档。名字相近，别混。

## 书写规则

五条，按顺序判断一段内容该不该写、写在哪：

1. **一份事实一个载体**：定义在 01，架构在 02，运维在 03，评测在 04；其他地方只放链接。
   同一件事出现两次，就是要修的错误。
2. **常青页 ≤ 200 行**（README ≤ 250）：超了就是该拆或该删，不是该继续写。
3. **开头三行**：读者是谁 / 读完能做什么 / 不读会怎样。写不出来就不该有这一页。
4. **不写历史**：常青页只写「现在是什么、为什么」；「以前是什么」进 CHANGELOG 或 git。
5. **代码即文档**：能被 lint/test 表达的（分层、接口、配置项、依赖清单）不写进文档——写了就会漂移。

第 1、2 条由 `ops/check-links.py` 的 `check_lengths` 机器检查；第 3–5 条靠 review，
但重复与历史痕迹在链接检查里也会暴露。
