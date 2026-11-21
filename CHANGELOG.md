# 变更记录

只记**批次级里程碑**。日常小修在 git history 里，不在这里。

这不是需要通读的文档：了解现状读 [docs/01-定义与决策](docs/01-定义与决策.md)～[04-评测与实验](docs/04-评测与实验.md)；
查某次改动的细节用 `git log` / `git show`。

> 2026-11-16 收敛：156 条 → 20 条。被删的日常记录在提交 `abe8234` 之前的 git 历史里。
> 文档总量的教训：流水账越详细，越没人读——它只需要在「想查某一批」时能被搜到。

## 2026-11-22 · 时间锚点：历史 / 总结 / 记忆写入不再各用各的「今天」

外部审查（GPT）定位到两条独立的日期缺陷，逐条复现后修。**为什么是同一批修**：
「历史进模型」和「记忆写入」各自有一个「今天」，只要一处不对，表现都是复述旧的
「明天」或换成一个错的具体日期。

- **历史进模型时丢了原消息时间**：`history()` 原来只取 `role, content`。模型知道
  「今天是 11-22」，却不知道旧消息是哪天说的，于是把 11-20 的「明天」复述成 11-23。
  现在带回每条消息的 `ts`，进 prompt 的副本标注 `[2026-11-20 21:00]`，并按**这条
  消息自己的时间**换算「明天/昨天/下周」。库里仍存原话（可追溯），只改送给模型的那份。
- **批量总结逐条换算**：`summarize.format_dialog` 每行带真实说话时间；一段跨天的对话
  里两句「明天」各自得到不同日期。总结 prompt 明确要求写 `YYYY-MM-DD`、计划写成
  「他说要」而不是已完成。
- **记忆写入不再拿写库当天猜日期**：`add_document` 新增 `anchor_ts`（来源对话时间），
  正文 / `key_facts` / 原子事实 / 跟进用同一个基准换算；没有来源时间就**保留原文**，
  不再用 `datetime.now()` 补一个确定的错日期（延迟总结时必然错）。documents metadata
  新增 `source_ts`，followups 表新增 `source_ts` 列。
- **主动消息用「说话时间」**：`proactive_sources` 的「约 N 天前他说过」改用
  `source_ts`；旧记录没有来源时不猜，说「之前」。即时记忆用消息落库时间做基准。
- **提醒授权时间不再按批准时刻重算**：`remind.freeze_when()` 在 `tools.execute`
  提交待授权时就冻成绝对时间，`/approve` 提示里显示同一时间；`_fmt` 改带年份。
- **日期输出带完整年份**：`timenorm.normalize` 输出 `2026-11-21（周六）`，补
  「昨天/前天/大前天」「今晚/明早/明晚/昨晚」；没有基准时间原样返回，不猜。
- **顺手修一处测试自伤**：离线回归里「同一条纠正只写一次」用 `LIKE '%生日%'` 数
  生产库副本，线上新增一条含「生日」的记忆后误红（2026-11-22 实测）；改成按精确
  文本数，测试不再随生产数据飘。
- **回归**：新增 `ops/test-dates.py`（37 项固定时间点：跨午夜 / 跨年 / 延迟总结 /
  无来源 / 原子事实与跟进一致 / 授权冻结），接入 `verify.sh --pre` 与全量、CI；
  prompt 快照基线随历史日期标注重签（`ops/fixtures/prompt-baseline.json`）。
  云端候选：日期锚点 37/37、离线回归 734/734、全链路 284/284、CI 清单 30/30。
## 2026-11-22 · 部署不再截断在途回复

- **线上发现**：01:38:10 的服务重启卡在「模型已生成 174 字、还没发送」之间，
  用户消息没有回复；健康检查 01:55 起报 `有消息 ... 没回`，02:00 连报 2 次并告警。
  日志证据：`[chat] req 9a4dc7 ... out=174` 后紧接 `Application shutdown`，
  没有 `[chat] sent`。
- **修法**：`release-switch.sh` 停服务前加 `wait_idle`：最近 120 秒内有用户消息、
  且最后一条 assistant 早于它时，最多等 60 秒让这一轮收尾；超时才切换。
  只拦「刚发出、还在生成」的轮次，不会因为几十分钟前的未回消息拖住部署。
- **已部署** `0cc56b8`：release 内 `--pre` 9/9、切换后全量 11/11。
  健康计数仍是 2，等下一条用户消息得到回复后自动归零。
- 旧 `health-cron.log` / `backup-cron.log` 里的 `Permission denied` 是 11-15 的历史记录
  （日志目标 mtime 停在 11-15），当前脚本可执行、每日备份与 5 分钟健康检查都在正常写日志。

## 2026-11-22 · 原子发布切换、投递确认与主人会话

复审 `6437c18` 后补的第二批边界：

- **发布切换改成整目录原子交换**：代码放进 `/opt/nonebot-releases/<sha>-<ts>/`；
  `data/venv/config.yaml/.env` 在 `/opt/nonebot-shared`，ops 日志/state/ops.env 在
  `/opt/nonebot-ops-state`。切换停服务后用 `renameat2(RENAME_EXCHANGE)` 一次交换
  `/opt/nonebot` 与候选目录，失败自动切回；每个版本在 `releases/` 留硬链接快照，
  回滚后回滚目标仍保留，新版本独有文件不会残留。
  **踩过的坑**：最初把 `/opt/nonebot/app_core` 做成指向 release 的符号链接，
  NoneBot 用 `Path.resolve().relative_to(cwd)` 算插件模块名，解析到 release 路径就
  抛 `ValueError`、服务起不来；所以 live 必须是真实目录，只能整目录换。
- **语音缓冲不再算已投递**：QQ/飞书 `defer_text` 分支只进缓冲、返回 False，
  runner 不写历史；`outbound.deliver_voice_reply` 在语音或文字兜底真正发成功后
  才写实际正文，两边都失败一个字都不写。
- **图片按实际投递记账**：`OutboundMessage` 增加 `images_sent/audio_sent`，runner
  回填发送结果；插件只在 `images_sent` 非空时记「另外发了一张图」。
- **主动结算统一时间窗**：逐条结算只看「发送后 REPLY_WINDOW 内」的用户消息，
  与 `reply_rate` 同口径；停机跨窗口不会被窗口外的一句回复整体免计。
- **主人会话统一**：`config.owner_session` 支持仅飞书主人，`private_session` 返回
  平台主人会话，实时总结改用 `private_session`；飞书主动消息与入站写同一条会话。
  没有可信主人会话时 `last_user_ts/reply_rate` 不再查所有用户，结算与 tick 跳过。
- **验证（本地）**：离线回归 **730/730**、runner **23/23**、批次 3 **27/27**、
  批次 4 **42/42**、恢复演练自检 **6/6**、分层反例 **11/11**、`smoke-all` **284/284**。
- **云端**：`e60491f` 已部署（release `e60491f-20261122-013735`），候选 `--pre`
  **9/9**、全量 **11/11**；真实回滚到 `a2c55ff-...` 再切回均 active。
  新布局下备份 `cloud-20261122-013911.tgz` + `restore-drill` 通过
  （documents 427 / memory_atoms 2115 / messages 2110）。

## 2026-11-22 · 权限边界、备份恢复与投递可靠性

按外部审查（基线 `26dc916`）修三批：身份与审批 → 发布与恢复 → 追问与投递。
这些是权限/数据/上线问题，不是提示词调优。

- **身份缺省不再放行**：`_is_owner` 在没有 `admin.user_id` / `admin.identities`
  时返回 False（旧行为是"谁都当主人"）。缺配时保留普通对话，关闭私有记忆、
  写工具与主动消息；启动自检和 `/ping` 明说当前是普通模式。插件侧统一走
  新公共入口 `core.is_owner(session)`，不再自己复制一套"空 admin 即主人"的判据。
  只有 `feishu.owner_open_id` 的部署也按可信平台主人处理（会话 `feishu:<open_id>`），
  配置自检不会把它误报成"admin 未配置"。
- **审批门故障即关**：`tools.execute` 在 `requires_approval` / `audit.propose`
  抛异常时**不执行写工具**并回一句"没有执行"；`propose` 落盘失败上抛，
  不再返回一个不存在的授权号。
- **待审批领取原子化**：`audit.decide` 先把请求从 pending 落盘删除，成功后才
  返回可执行项；落盘失败返回 None，同一 ID 不会重放。propose/decide 共用
  进程内锁，避免并发覆盖。故障注入用例覆盖"登记失败零执行 / 领取失败不返回"。
- **恢复演练按清单判**：`cloud-backup.sh` 生成 `manifest.json`（逐文件
  sha256/字节数 + 必需库表行数），缺任一必需库直接失败。`restore-drill.py`
  逐一核对清单与必需库；缺库、哈希不符、备份有数据恢复为空都失败，
  备份时就是 0 行则记 `empty_by_design` 通过；无清单的旧备份按严格模式。
  新增 `ops/test-restore-drill.py` 六种场景自检，接进 `verify.sh --pre`。
- **部署先验证再切换**：`deploy.sh` / `deploy.ps1` 改为解到候选目录、
  在候选目录跑 `verify --pre`，过了才切换；快照覆盖完整发布范围（含
  `ops` 与 `.deployed_sha`），版本标记在切换时更新；修掉删除清单多拼一层
  `nonebot/` 的路径错误。`--no-restart` 不再覆盖生产目录。
- **主体导出区分缺省/空/读失败**：损坏的 SQLite 源记为 `failed` 并写进
  manifest 的 `errors`，导出返回失败、`--verify` 也不通过；空环境仍可
  导出（`missing`），并校验条目数与清单一致。
- **主动追问逐次结算**：`proactive_log` 增加 `settled_ts` / `ignored`，
  每条发送单独判断"有没有回应"，同一话题两次不回会记两次；状态落盘，
  重启不重计。升级时只迁移升级前的历史行，不把新发送吞掉。
- **跟进归因收窄**：短答复（"还没"/"还在弄"）绑定到最近实际追问的那条；
  无关完成句（"我把饭做完了"）不再关闭数据库脚本待办；状态更新同时刷新
  `answered_ts`，14 天冷却看得到这次回应。
- **历史只写通道确认过的内容**：流式尾段在收尾阶段发送并按返回值记账；
  runner 在发送成功后统一写历史（deferred），首段成功尾段失败只记首段，
  全失败不写；平台发送/typing 加超时，某段挂死时本轮能收尾、下一轮能继续。
  QQ 超时先发"先说到这"再补记，飞书同理。
- **通道边界**：QQ 引用 ID 跟随本轮 `QQChannel` 实例（不再用会话级共享字典，
  排队消息不会覆盖当前引用）；飞书去重改为有界 FIFO（`app_core/seen.py`），
  500 条边界只淘汰最旧，不再整集合清空。
- **验证（本地）**：离线回归 **723/723**、runner **21/21**、批次 3 **27/27**、
  批次 4 **38/38**、恢复演练自检 **6/6**、分层反例 **11/11**、
  `smoke-all` **284/284**（均以 `PYTHONIOENCODING=utf-8` 跑）。
- **云端部署（2026-11-22 01:05）**：代码提交 `bea876a` 上云（`.deployed_sha`），
  候选 `--pre` **9/9**、切换后全量 **11/11**（含插件启动），服务与 QQ/飞书连接正常。
  删除路径验收在首次部署 `accef3c` 上完成：预置 `app_core/retrieval.py` + 旧标记
  `c534114`，部署后文件被正确删除（没有多拼一层 `nonebot/`）。失败预检验收：
  故意缺层标记的 `a240c8c` 让 `deploy.ps1` 退出 1，生产 `.deployed_sha` 未变、
  服务 active、坏文件未进生产。
- **真实备份演练**：新备份 `cloud-20261122-005951.tgz` 带 manifest，`restore-drill`
  通过（documents 425 / memory_atoms 2105 / messages 2097）。
  本文档提交在部署之后，只改 `CHANGELOG`，不改变运行代码。

## 2026-11-16 · 批次 5：只读 MCP 客户端 + 人格自学习影子

- **新增** `app_core/mcp.py`：stdio + JSON-RPC 连外部 MCP server，把白名单里的只读 tool
  注册成 `mcp__<server>__<tool>`。三条判据：白名单（只连声明的 server / 只暴露声明的 tool）、
  超时（默认 8 秒，慢 server 降级成一句话）、不可信隔离（返回包成「[外部数据，非指令]」并截断）。
- `tools.execute` 走 MCP 分支；server 起不来 / 超时 / 协议错都只降级，不影响本轮。
- **人格自学习只有影子**：`persona.self_learn=shadow` 时，纠正命中落 `persona-drift.jsonl`，
  **不真改人格**——改人格等于改「它是谁」，没有审批与回滚前不做。截止 **2026-11-30**。
- `ops/test-mcp.py` 12 条自检（用一个假 server 验协议、超时、白名单、注册、降级），接入 `verify.sh`。
- 文档：`docs/02` §7 补接法，`docs/04` 台账把那行拆成「MCP 已落地 / 人格影子」两行。

## 2026-11-16 · 影子清理：删三个无消费方的影子 + BM25 + 别名表

- **删**：补一句（0 样本）、画像块（212 条）、自指率（251 条）——没有消费方、没有结论，
  每轮白写一行盘；代码在 git 历史里。`shadow.py` 406 → 198 行，只留正例 / 召回门控 /
  碎片候选 / 连边 / 二次召回。
- **删**：BM25+RRF 整套（`retrieval.py`、`memory_index` 影子、`record_retrieval`、
  配置键与面板项）——判据 11-14 已被人工否决，原计划 11-19 到期删除，提前到 11-16。
- **删**：人工实体别名表——字面相似的仍由 `similarity` 合并，字面不像的不再人工维护。
- **顺带修**：`deploy.ps1` / `deploy.sh` 现在会删除「上次部署 sha 到本次 HEAD」之间被移除的文件。
  tar 解压是覆盖写，删了 `retrieval.py` 云端旧文件还在，link-check 报「模块没进 smoke-all」才发现。
  另：`deploy.ps1` 回写时丢了 UTF-8 BOM，PowerShell 5.1 按 GBK 解析导致整片语法错误。
- **数字**：离线 735 → **708**、全链路 288 → **281**——删掉的用例对应删掉的功能，不是覆盖退化。
  云端 `verify.sh` **9/9**。

## 2026-11-16 · 文档载体收敛：删台账、CHANGELOG 收敛、README 重写

- **删** docs/05-改动台账.md：和 CHANGELOG 记同一件事（改了什么 / 验证数字），两份必然漂移；
  内容在 git 历史里，引用处同步改为「见 git 历史」。
- **CHANGELOG 156 条 → 20 条**批次级里程碑；头部写清「这是流水，不需要通读」。
- **README 重写**：删掉按功能罗列的「特性」表，改成叙事——它和聊天机器人的差别、
  判断/记忆/出场三支柱、一轮对话（mermaid 图，跟随代码）、接通道、改/部署/验证。
  数字与时限更新到最新（13 个工具、90/30/10 时限、735/288）。
- **书写规则**写进 docs/README.md：一份事实一个载体、常青页 ≤200 行（README ≤250）、
  开头三行、不写历史、代码即文档。
- ops/check-links.py 新增 check_lengths 把第 1、2 条变成机器检查；
  docs/03 因超 3 行被这条规则逼着删了两段历史性描述。
- 英文版顶部标注「可能滞后，以中文为准」；备份/ 目录排除在文档检查之外。
- **验证**：check-links PASS（22 篇 / 80 链接 / 84 处引用 / 8 篇篇幅）；云端 verify.sh 9/9。

## 2026-11-16 · 重构批次 P4/P5（复盘基线、存储格式设计稿、依赖清单）

- **P4 低成本版**：`chat_turn.py` 新增 `TURN_DEPS`，显式列出 ChatTurnMixin 对 ChatCore
  的 19 个依赖；`ops/lint-plugins.py` 的 `check_turn_deps` 双向检查——
  新增未登记 `self.xxx` → FAIL，`TURN_DEPS` 里的成员在 ChatCore（含三个 Mixin）上找不到 → FAIL。
  反例验证：把 `self.seg_max` 换成 `self.nope_dep`，lint 立刻红。
- **P5 设计稿**：`docs/04` §9 记忆存储格式——**文件是真相源、SQLite 是派生索引**，
  写路径先落文件再更新索引、读路径只读索引保持延迟，分双写 / 切读 / 删表三步迁移，含判据、门槛、回滚。
  另 §10 记录 TurnContext 完整版为什么现在不做（收益是可测性，成本 40+ 处 diff）。
- **P2 验收工具**：`ops/ab-review.py` 用 3 组固定输入 + 固定 day 跑 `apply_review`，
  对 LEARN 下全部文件做 sha256 基线；profile.json 的 `updated_at/seen/previous.at` 归一化，
  CRLF/LF 归一化——**Windows 生成的基线在云端 Linux 也成立**。接入 `verify.sh` 与 CI。
- **验证**：云端 `verify.sh` **8/8**（新增复盘基线）；离线 735/735、全链路 288/288。
- **过程中被拦住的问题**：`profile.json` 带时间戳导致基线每次跑都变（两轮才归化全）；
  Windows CRLF 与云端 LF 基线不兼容；`TURN_DEPS` 的成员分散在三个 Mixin 文件里（检查漏扫）。

## 2026-11-16 · 重构批次 P3（插件门面：29 条业务 import 归零）

- **改了什么**：
  - 新增 `app_core/plugin_api.py`：16 个命名空间对象（`learn/audit/kb/wiki/diary/meme/
    mood/remind/profile/help/introspect/tools/scheduler/proactive_*/relational`），
    `ChatCore.__init__` 挂成实例属性 —— 插件写 `core.learn.review(core)`，不再 import 业务模块。
  - `CORE_API` 扩成两层（`core.<ns>.<func>` 逐条登记），检查区分命名空间与普通属性：
    `core.persona.get` 只认单层，`core.learn.review` 必须登记。
  - 6 个插件（admin/commands/schedule/proactive/chat/feishu）29 处 import 全部迁完，
    `PLUGIN_LEGACY` **清空**——以后插件直接 import 业务模块一律 FAIL。
  - **环检查改为只看顶层 import**（lint-layers 与 link-check 两个工具）：
    `chat.__init__` 里 `import plugin_api` 是运行时解耦，不是结构环；
    与 link-check 注释里「函数内 import 能绕」的判据对齐。
- **为什么**：29 条白名单带 2026-12-31 到期日，不清就会在到期日当天 CI 全红；
  且插件直接 import 业务模块会把内部结构焊死。
- **验证**：云端 `verify.sh` **7/7**；离线 735/735、全链路 288/288、`test-commands` 11/11、
  插件启动 7/7。
- **过程中被拦住的 3 个问题**（都在重启前）：
  两层正则把 `core.persona.get` 误判成未登记接口；
  环检查把函数内 `import plugin_api` 当成结构环；
  批量替换漏掉 `introspect/remind` 前缀和 chat 整行删除导致的 `config` 丢失。
- **没做的**：P4 TurnContext（37 个 self 访问点）、P5 存储格式设计文档、ab-review diff 工具。

## 2026-11-16 · 重构批次 P1/P2（harness 收尾、learn 解环）

- **P1 · 机械清理**：harness 再迁 20 个脚本（affect/asr/commands/dialog/fact-check/
  memory-port/runner/self-check/web + console/e2e/guards/slot），删掉各自的 check 定义与尾部汇总。
  剩余 9 个不迁（batch 用 TOTAL/FAILS、check-links/embed 用 FAILS、golden-m0 有 rc==2 分支、
  nonebot-offline 3566 行）。**面板键表本来就是从 DEFAULTS 生成的**（`build_schema()` 遍历
  `DEFAULTS`，`F` 只覆盖说明），这项不需要改。
- **P2 · learn 解环**：
  - 抽 `notes_meta.py`：笔记页元数据 9 个纯文件解析函数，wiki 改为直接依赖它 —— **learn↔wiki 环消失**。
  - 抽 `learn_constants.py`：5 个复盘 prompt 常量，review 不再从 learn 拿。
  - 抽 `learn_store.py`：路径常量、画像字段读写、清单读写、`apply_review` 落盘、
    文本去重规则（353 行），review 改为 `import learn_store`，调用点前缀不变 —— **learn↔review 环消失**。
  - `learn.py` **833 → 303 行**（业务只剩：技能、笔记写入 + `_touch_wiki`、间隔重复、归档、复盘入口）。
  - `CYCLE_BASELINE` 与 link-check 的 `_ALLOWED_CYCLES` **清空**：app_core 现在没有 import 环。
  - `_touch_wiki` 保持同步直调（采纳「不引入标记文件」的建议，没有用延迟索引换架构）。
- **验证**：云端 `verify.sh` **7/7**；离线 735/735、全链路 288/288。
- **过程中被拦住的 4 个问题**（都在重启前）：
  link-check 的环基线还是 learn-review-wiki 三角（SCC 变了被判成新环）；
  `learn_constants` 缺 `profile` 导入；`profile.FIELDS` 触发红线 8 需登记；
  测试/烟雾对 `learn.LEARN/PROFILE/CORRECTIONS/CURIOUS` 的补丁点必须跟着 store 走（两轮才迁全）。
- **没做的**：P3 组合门面（`core.learn / core.audit / core.kb`）、P4 TurnContext、P5 存储格式。

## 2026-11-16 · 重构批次 B3（通道胶水上移 outbound）

- **改了什么**：QQ 与飞书两个插件各写一遍的 `_speak_later` / `_draw_later` /
  `_deliver_voice_reply` / `_read_aloud_requested`（约 141 行重复）收到
  `app_core/outbound.py`：通道只提供 `synth` / `send_voice` / `send_image` /
  `send_text` / `record` 五个回调，TTS 格式由传入的 `synth` 决定（QQ silk / 飞书 opus）。
  插件侧函数名与调用点不变，只把函数体换成委托。
- **为什么**：接第三个通道的成本从「再抄一遍 140 行」降到「实现 5 个回调」；
  两版逻辑曾经漂移过（飞书删临时文件、QQ 不删），上移后只有一份。
- **验证**：云端 `verify.sh` **7/7**；离线 735/735、全链路 288/288 不变。
  真机语音听感仍需人工验收（离线测不到）。
- **没做的**：面板键表自动生成。`config-ui.py` 的 `F` 已有 `F.get(fid, ...)` 兜底，
  缺键只是没说明；自动补会把 bool/int 渲染成纯文本，风险大于收益，留给面板下次改动。

## 2026-11-16 · 重构批次 B3b（配置三处同步）

- **改了什么**：
  - 新增 `ops/check-config-sync.py`：`config.example.yaml` 有、`config.DEFAULTS` 没有的键
    一律 **FAIL**（写了等于没写，代码和面板都不认）；反过来只 WARN。接入
    `verify.sh` 三个模式与 CI。
  - 补两个**真实漂移**：顶层 `weather`（`perception.py` 在读、`DEFAULTS` 没有，靠
    `or {}` 兜底）、`llm.prepare_parallel`（yaml 写在 `llm` 段、代码读 `chat` 段，
    这个开关**从来没生效过**——默认恰好是 false/串行，所以一直没被发现）。
- **为什么**：同一个事实在 DEFAULTS / yaml / 面板 / 测试四处手抄，weather 那次已经漂了。
  配置项「四处登记」是文档要求，之前没有机器检查。
- **验证**：云端 `verify.sh` **7/7**（规范 / 分层 / 配置同步 / 离线 735 / 联动 / 全链路 288 / 插件启动）。
- **没做的**：面板键表仍手写（检查器只 WARN）。真正「单向生成」要改 `config-ui.py` 的
  键表结构，风险大于收益，留到面板下次改动时一起做。

## 2026-11-16 · 重构批次 B2（拆入口：chat.py 1456→547，chat_turn.py 1106→704）

- **改了什么**：
  - **chat.py 切成四个 Mixin**：`summary.py`（摘要/水位/矛盾/追赶，9 个方法）、
    `history.py`（历史读写/身份/记消息/prompt 账，10 个方法）、`chat_reply.py`
    （回复主链路/工具循环/风格决策/用量统计，15 个方法），`ChatCore` 只剩装配、
    DB 初始化、记忆与情绪端口、人格加载——**1456 → 547 行**。
  - **chat_turn.py 拆两层**：`chat_turn_base.py`（Turn/Slot/StageError/阶段常量，
    201 行）+ `chat_turn_commit.py`（收尾四段，290 行），ChatTurnMixin 只剩
    准备/组装/生成——**1106 → 704 行**。
  - **`_turn_commit` 235 行拆成四段**：写库 45 / 记忆 42 / 观测 57 / 收尾 93，
    由 `_turn_commit` 顺序调度（顺序不能换）。
  - **lint 跟随拆分**：`check_remember_stage` 放行 `_commit_*` 前缀；
    SLOTS 允许在 `chat_turn_base`；`summary.py` 进 `_MEM_EXEMPT`（chat 的装配点）；
    `chat_turn_commit.py` 的 `memory.embed` 存量登记。
- **为什么**：入口类 53 个方法、单函数 235 行，改一处要通读 2500 行；
  B0 的护栏已经把「回退」挡住，这一步才敢搬。
- **验证**：云端 `verify.sh` **6/6**（规范 / 分层 100 模块 / 离线 735 / 联动 / 全链路 288 / 插件启动）；
  `prompt-golden` 39/39。期间 pre-verify 抓到 3 次 NameError（Mixin import 无括号、
  SUMMARY_BATCH 常量、DeliveryPolicy 导入），都在重启前拦住，未带病上线。
- **没做的**：TurnContext 显式化（把 Mixin 的 self 换成一等上下文）。切片先做，
  显式化等切片稳定后单独评估——一次只改一件事。

## 2026-11-16 · 重构批次 B0/B1a/B3a（分层护栏生效、密钥统一、四组壳环拆掉）

- **改了什么**：
  - **B0a 分层标记全覆盖**：`lint-layers.py` 改为从模块 docstring 首行的
    `[domain]` / `[orchestration]` / `[infra]` 推导层，删掉手写的 DOMAIN/ORCH 名单
    （它和标记是两套事实，94 个模块里只有 49 个被覆盖）。58 个文件补/修标记；
    `asr` 由 domain 改 infra（HTTP 客户端），`insight` / `memory_instant` 改
    orchestration（写记忆）。新增 `ops/test-lint-layers.py` 11 条反例：缺标记、
    domain→编排、domain→IO、infra→编排、kernel→业务、新增环、插件→业务，
    每条都确认会红。
  - **B0b 插件 import 收口**：`lint-layers.check_plugins()` 此前是 `pass`
    （文档写着「由 lint 强制」）。现在生效：插件只能 import 通道协议与基础设施
    （engine/runner/delivery/message/pacing/turn/files/asr/tts/image_provider/kernel），
    业务实现按文件冻结 29 条（`PLUGIN_LEGACY`），2026-12-31 到期后一律 FAIL。
  - **B1a 拆四组壳环**：`followup` / `mood` / `remind` / `scheduler` 四组
    「壳持有 DB 路径、store 反向动态读壳」的循环，改为路径定义在 store、壳 re-export；
    测试补丁点同步移到 store（`followup_store` / `mood_store` /
    `scheduling.remind` / `scheduling.scheduler`）。循环依赖 **6 组 → 2 组**
    （只剩 learn↔review、learn↔wiki，留 B1b）。`lint-layers` 新增
    `CYCLE_BASELINE`：新增环 FAIL，基线里的环消失时 WARN 提醒收线。
  - **B3a 密钥统一**：10 处各自 `json.loads(paths.KEYS…)` 收到
    `app_core/kernel/keys.py`（mtime 缓存 + `get`/`section`/`require`），
    轮换密钥不再只有 llm 生效。
  - **测试骨架**：新增 `ops/harness.py`（check/report/bootstrap/temp_root/copy_db），
    approval / engine / delivery / kernel / identity / inbound / package 七个脚本先迁移。
  - **部署工具**：本地没有 bash，新增 `ops/deploy.ps1`，门禁顺序与 `deploy.sh` 相同
    （快照 → 上传 → `verify.sh --pre` → 重启 → 全量 verify），不做任何跳过。
- **为什么**：复盘发现三件事——分层检查只覆盖 49/94 且最近三批新代码恰好落在
  未覆盖侧、`check_plugins` 空转、6 组循环依赖把「改一个模块」变成「读五个模块」。
  这批不改行为，只把边界变成可执行的约束，让后续拆分有红绿线。
- **验证**：`lint-layers` PASS（95 个模块全标记）、`test-lint-layers` 11/11；
  云端 `verify.sh --pre` 4/4、全量 6/6 通过。期间 pre-verify 抓到 1 条真回归
  （拆环后调度器测试仍补壳常量，`missing_jobs` 查到空库），修测试补丁点后复跑全绿，
  **没有带病重启**。
- **顺手修**：`test-memory-port` 的 FakeMemory 缺 `source_msg_ids`（批次 4 加原文指针后
  没同步假实现，云端专项跑出来）；`test-package` 在部署目录找不到仓库根的
  `pyproject.toml`，改为存在才检查（CI 仍覆盖）。
- **事故与恢复**：`test-dialog` 没有临时根隔离，直接 `unlink` 生产的
  `data/relation.md`。核对备份发现这份 250 字的关系小抄 **11-14 15:51 起就已经是
  0 字节**（不是本批弄丢的），已从 `cloud-20261114-031701.tgz` 恢复并核对内容；
  `test-dialog` 现在把 `paths.DATA` / `followup_store.DB` / `mood_store.STATE`
  全部指向临时目录，不再触碰生产文件。
- **没做的**：B2 拆入口（TurnContext + 四段物理切片）、B1b 拆 learn、B3b 配置生成
  与通道胶水上移——按计划在下一批，本轮不碰 `chat.py` / `chat_turn.py`。

## 2026-11-16 · 批次 4：C 可授权（审计层）与体验收尾

- **改了什么**：
  - **审计层（C 的地基）**：核对后发现 11-10 删 `agentd` 留下的「先有审计层」前提
    **从未兑现**——`approval.py` 只是内存 Future，无队列 / 落盘 / 超时，也没有审批命令。
    新增 `app_core/audit.py`：写类副作用逐条记 `data/audit.jsonl`（`prev_hash` 哈希链、
    可回滚标记、状态追加不覆盖），`propose/decide` 管理 10 分钟过期的待授权请求；
    `/audit` 查看、`/approve <id>` 批准后才由 `tools.execute(..., approved=True)` 重放执行。
    `tools.require_approval` **默认关**：旧行为不变，但每次写操作仍记审计。
    `meme_send` / `web_open` 明确标「不可回滚」，不假装能撤销。
    **本批不新增自主执行能力**，只补责任边界。
  - **修真 bug**：`web_open` 会 `learn.note_write` 往知识库写笔记，却被列进
    `READ_ONLY_TOOLS` 参与同轮并发；已从 `tools.py` / `tool_loop.py` 两处名单移出，改走串行 + 审计。
  - **#15 多段语音**：`pacing.split_for_voice()` 按句切 300 字一段（最多 8 段，
    纯规则、拼接不丢内容）；QQ 与飞书的 `_speak_later` / `_deliver_voice_reply`
    改为逐段合成发送。单段路径与旧行为一致，不再把 300 字之后的内容丢掉。
  - **#14 飞书主动消息**：新增 `app_core/proactive_channels.py` 注册表；飞书
    `_chat_of` 从「只写不读」改为落盘 `data/feishu-chats.json`（重启不丢），
    新增 `_send_proactive` 并写回历史；`proactive.channel` 默认 `qq`，
    改成 `feishu` 时优先飞书、发不出去自动回退 QQ，**不双发**。
  - 护栏与文档：`ops/test-batch4-guards.py`（30/30）；`help.py` 登记两条命令
    （离线回归「展示清单已精简」阈值 13 → 14，仍卡住继续膨胀）；
    CI 与 AGENTS §1 登记；`docs/01` C 落地、`docs/02` 默认值与并发名单、
    `docs/03` 审计命令 / 主动通道 / 已知待办划掉两项、`docs/04` 台账。
- **为什么**：定义 01 §3 的 C 要求「每次副作用可追溯、可回滚、需授权」，
  而既有代码只能回答「哪个工具被调用过」；早先判断「审批零件基本齐了」是错的，
  这批改成了从零补齐。多段语音与飞书主动消息是路线图上挂了两周的已知限制。
- **明确推后**：只读 MCP 客户端、人格自学习受控实验。仓库里 MCP 代码为零、
  画像变更无审批链；两者正好依赖本批刚落地的审计层，排到批次 5 再定判据与截止日。
- **验证**：本地 `check-links` 21 篇 / 92 链接 / 92 处路径引用 / 5 项登记 PASS，
  `lint-plugins` FAIL 0 / WARN 0、`lint-layers` PASS、`test-guards` 4/4、
  `test-check-links` 6/6、`test-embed-guard` 12/12、
  `test-batch2-guards` 24/24、`test-batch3-guards` 24/24、`test-batch4-guards` **30/30**、
  `py_compile` 全过、`scan-secrets` 238 文件无问题。云端**不重启预检**首轮抓到
  离线回归 1 条 FAIL（新增两条展示命令使清单 12 → 14，超过旧阈值 13），
  调阈值并注明原因后复跑：全量 `verify.sh` **5/5**、`test-batch4-guards` 30/30、
  `test-commands` 11/11、`test-config-ui` 48/48、`test-prompt-golden --offline` 39/39。
- **部署**：一次通过（`6f02907`）：`verify.sh --pre` 3/3 → 重启 `active` → 全量 5/5；
  `.deployed_sha=6f02907`，回滚点 `/opt/backup/code-20261116-171111.tgz`。
  逐项结果见 git 历史 #6。多段语音 / 飞书主动消息 / 授权门
  均需真机验收（云端 verify 测不到体感与外部通道）。

## 2026-11-16 · 批次 3：B 可携带与可观测（主体导出、只读面板、预算开关）

- **改了什么**：
  - **主体导出（B 落地）**：新建 `ops/export-subject.py`，把人格 / 记忆 / 画像 / 关系 /
    纠正 / 跟进合成一个带 `manifest.json`（每片 sha256 / 字节数 / 条目数）的目录；
    JSON 排序缩进、JSONL 每行稳定、统一 LF，可直接进 git diff；`--verify` 做往返自校验。
    刻意不导出向量 blob（换 embedding 后跑 `rebuild-vectors.py --apply` 重建）、
    `keys.json` / `config.yaml`、聊天历史。旧的 `export-memory.py` / `export-persona.py`
    保留，主体快照是它们之上的完整层。
  - **#30 本地可观测面板**：`status.observability()` 汇总健康快照 + 最近 12 轮
    `memory_trace`（注入哪条 / 原始 score / 耗时 / 门控判断）+ 影子日志行数体积 + 关键开关；
    `config-ui.py` 加只读 `GET /api/observability`，前端加「状态 / 轨迹」tab 并列出影子截止日。
    仍只绑回环、需 token、**不写任何数据**。
  - **#7 分层注入预算**：`prompt._compact_background` 加 `budget_mode`，`tiered` 给
    fact / insight 各自配额（推断不再挤占「他说过的话」的名额）；`chat.budget_mode=legacy`
    默认，legacy 路径逐字节不变。可逆开关，按 01 §6 不设影子，转正前跑 prompt A/B。
  - **#9 访问强化接线**：复核发现按 importance 分档软归档（180 / 90 / 30 天）、
    `profile.py` 字段级过期、`restore()` 可捞回**早已实现**，不再重复造轮子；本批只把
    `memory_atoms.reinforcement_count / last_reinforced_at` 接进 `touch()`（纯记账，不改状态、不删数据）。
  - **#8 碎片整合候选**：`_shadow_dedup` 命中不再只打日志，落
    `data/consolidation-candidates.jsonl`；新建 `ops/consolidate.py` 只读汇总（去重对数、
    cos 分档、同文重复数）。**不写库**：合并成自足条目需调模型且不可逆，按 01 §6 设截止 2026-11-30。
  - 护栏与文档：`ops/test-batch3-guards.py`（24/24，含导出篡改 / 缺片反例、预算双模式、
    面板数据解析、只读性）；CI 与 AGENTS §1 登记；`docs/01` B 落地说明、`docs/02` 默认值、
    `docs/03` 面板与导出命令、`docs/04` 影子台账两行。
- **为什么**：定义 01 §3 的 B 明确写「SQLite 里的记忆不算可携带」，但仓库原来只有两个各导一半、
  无清单无自校验的脚本；「可审查」是陪伴产品的信任底座，以前排障只能翻 journalctl / prompt_log；
  #9 复核后确认主体机制已在，硬加一遍反而会重复归档逻辑。
- **验证**：本地 `check-links` 21 篇 / 92 链接 / 92 处路径引用 / 5 项登记 PASS，
  `lint-plugins` FAIL 0 / WARN 0、`lint-layers` PASS、`test-guards` 4/4、
  `test-check-links` 6/6、`test-embed-guard` 12/12、`test-batch2-guards` 24/24、
  `test-batch3-guards` **24/24**、`py_compile` 全过、`scan-secrets` 235 文件无问题、
  `node --check app.js` 通过。云端**不重启预检**：全量 `verify.sh` **5/5**、
  `test-config-ui` 48/48、`test-batch3-guards` 24/24、
  **`test-prompt-golden --offline` 39/39**（legacy 字节未变）。
- **部署**：一次通过（`4bea4c6`）：`verify.sh --pre` 3/3 → 重启 `active` → 全量 5/5；
  `.deployed_sha=4bea4c6`，回滚点 `/opt/backup/code-20261116-164654.tgz`。
  逐项结果见 git 历史 #5。面板是回环只读页，界面渲染需人工验收。

## 2026-11-16 · 批次 2：A 主体连续性（超时语义、检索门控、原文指针、确定性裁决）

- **改了什么**：
  - **#34 超时语义**：`runner.py` 去掉 `asyncio.wait_for(45)` 一刀切，改为 watchdog 双层时限——
    `reply_deadline_seconds=90` 宽松总上限只兜底，`reply_stall_seconds=30` 无进展超时
    （发出首句后久无新句子才收尾），取证 / 首字阶段不受 stall 约束；取消仍转成
    `TimeoutError` 走原兜底。`chat.py` 工具预算改从 `first_token_budget` 派生
    （旧配置下数值仍 40s / 15s），不再被总闸绑架。三个通道统一从 core 读 deadline + stall。
    QQ 超时已发段落补记历史后加收尾「先说到这。」，不再让用户「再说一遍」；
    飞书超时补记已发文字并 `return`——顺手修掉一个真 bug：超时分支里 `out` 未赋值
    却在下一行被 `getattr(out, "images")` 读取，会二次抛 `UnboundLocalError`。
  - **检索门控（新影子）**：新增 `app_core/recall_gate.py`（纯规则、拿不准一律放行），
    `memory.recall_gate=off|shadow|on` 默认 off；shadow 落 `shadow-recall-gate.jsonl`
    （判断 + 实际召回条数），on 才跳过 fact 召回。依据 RealCompanion 2026-10：真实流量
    仅约 3.4% 消息需要回忆、检测器 AUROC 0.530，瓶颈是「何时不检索」。截止 **2026-11-30**。
  - **#5 原文指针 + trace**：`add_document` 新增 `source_msg_ids`（即时记忆写入本轮
    `msg_id`）；`memory.recall()` 返回原始余弦分，经 `MemoryPort.payload` 传到
    `memory_trace` 的 `injected[].score`；补 `deleted / probe_id / t_recall_ms`。
  - **#6 裁决确定性化**：`contradict.prefilter()` 在送模型前挡掉「文本完全相同 /
    同水位区间 / 时序倒挂」，被筛掉的也落 `contradictions.jsonl`（reason 前缀
    `prefilter-`）；LLM 只判语义是否冲突，结果可复现且省一次调用。
  - **#35 并发不变量**：复核确认每轮状态已在 `Turn`、实例只留配置 / 惰性单例 /
    `_summarizing` 与模块级 `_mood_lock`（均为有意共享），无需重构；改为固化护栏
    `ops/test-batch2-guards.py`，静态禁止把 trace / turn 挂回实例并带反例自检。
  - 护栏与文档：新增 `ops/test-batch2-guards.py`（24/24），`ops/test-runner.py` 加 6 条
    无进展超时用例（17/17），`smoke-all.py` 覆盖新模块与 `prefilter`（联动体检要求
    `app_core` 每个模块都被 import）；CI 加一步；同步 `docs/01` §6、`docs/02` 时限与默认值、
    `docs/04` trace 清单与影子台账、AGENTS §1。
- **为什么**：45 秒是从「工具 6 轮最坏 9 分钟」倒推的兜底，却反过来把长回复 / 多段语音
  切成半截、还让用户重说，与 A「关系连续性」直接冲突；真实陪伴流量里稀缺的是
  「什么时候不该回忆」而不是更多检索通道；矛盾裁决的时序比较本就该由代码判。
- **验证**：本地 `check-links` 21 篇 / 92 链接 / 87 处路径引用 / 5 项登记 PASS，
  `test-check-links` 6/6、`lint-plugins` FAIL 0 / WARN 0、`lint-layers` PASS、
  `test-guards` 4/4、`test-embed-guard` 12/12、`test-batch2-guards` 24/24、
  `py_compile` 全过。云端**不重启预检**（正式部署前）：全量 `verify.sh` **5/5**、
  `test-runner` 17/17、`test-batch2-guards` 24/24、`test-config-ui` 48/48。
  首次预检曾抓到联动体检 FAIL（新模块 `recall_gate` 未进 smoke-all 覆盖），已修。
- **部署**：一次通过（`aa82ffc`）：`verify.sh --pre` 3/3 → 重启 `active` → 全量 5/5；
  `.deployed_sha=aa82ffc`，回滚点 `/opt/backup/code-20261116-162041.tgz`。
  逐项结果见 git 历史 #4。超时体感属对外行为，云端 verify 测不出，
  **仍需真机验收**（长回复 / 多段语音 / 卡住时的收尾）。

## 2026-11-16 · 批次 1：embedding 维度守卫、影子收尾、配置对齐

- **改了什么**：
  - **#1** `Memory.__init__` 改由 `embed_provider.spec()` 解析 provider / api / model / dim
    （以前只读 api/model，dim 常为 0，写侧不校验）；新增读侧 `check_dim()`，
    召回维度不符时记 `log.error` + `alerts.jsonl`（code `embed_dim_mismatch`），
    降级仍保留但**不再静默**。`embed()` 的 httpx 改为延迟导入，spec/check_dim 可离线测。
  - **#2** 新建 `ops/rebuild-vectors.py`（默认 dry-run，`--apply` 才重算并写回），
    修 `config.example.yaml` 里指向不存在的 `ops/rebuild-embed.py` 的注释。
  - **#3/#4/#31** `config.py` 与 `config.example.yaml` 的 `shadow_retrieval` 默认改 false
    （与代码注释一致）；`shadow.retrieval_line()` 改为读真实开关而非硬编码「已停止」；
    面板标注停用；`docs/04` 台账写明影子代码 11-19 到期删除。
  - **#36** `adapters/console.py` 把 `core.reply_deadline` 传给 `TurnRunner`，
    终端通道不再吃 runner 里硬编码的 45。
  - **#23** `embedding.provider/dim`、`memory.recall_repeat_window_min/penalty`
    登记到 DEFAULTS、example 与面板键表；**#22** `ops/probe-recall.py` 记忆阈值改为读
    运行时 `recall_min_score`，不再写死 0.25。修 `AGENTS.md` 红线 8「mood/task 未进契约」的过时描述。
- **为什么**：换 embedding 家后旧向量与查询不在同一空间，原来会静默退化成 recent()，
  用户只看到「它记不住了」而日志里什么都没有；配置、状态页、注释、探针四处口径不一，
  11-19 到期的影子实验还开着。
- **验证（本地）**：`py_compile` 9 个改动文件 OK；新增 `ops/test-embed-guard.py` **11/11**；
  `check-links.py` 21 篇 / 92 链接 / 82 处引用 / 5 项登记 PASS；`test-check-links` 6/6；
  `lint-plugins` FAIL 0 / WARN 0；`lint-layers` PASS；`test-guards` 4/4。
  离线回归与全链路本地缺依赖未跑，由云端 `verify.sh` 覆盖。
- **验证门拦了三轮**，三个独立根因都在测试侧的假向量，**生产写侧维度校验始终保留**：
  1. `verify.sh --pre` 召回 golden 2 条 FAIL：读侧 `check_dim` 误把 `self.dim=2048`
     与调用方传入的低维 `qv` 比较，正常召回被降级成 `recent()`。改为「读侧只比矩阵
     与查询维度，配置维度交给写侧 `embed()`」，并补一条护栏用例。
  2. 离线回归直接抛 `EmbedError`：`test-nonebot-offline.py` 的 mock 返回 2 维假向量。
     改为只在该用例实例上把 `dim` 置 0（该用例只验空文本过滤）。
  3. 第 3 次 `--pre` 3/3 通过、服务已重启，但全量 verify 的 `smoke-all.py` 4 项
     `EmbedError`（HTTP mock 返回 3 维 `VEC`）：改为按 `embedding.dim` 生成同维假向量，
     `emb[0][:3] == VEC` 的断言不受影响。
  前两次未重启，第三次重启后全量仍红；每轮都先修再走。修复后云端先跑
  `verify.sh` 全量 **5/5 通过**，再正式部署。
- **部署**：`ops/deploy.sh` 已执行，`HEAD eb7872a` 上云；`verify.sh --pre` **3/3**、
  重启后全量 **5/5**（分层 / 离线 / 联动 / 全链路 / 插件启动），服务 `active`，
  `.deployed_sha = eb7872a`，回滚点 `/opt/backup/code-20261116-155432.tgz`。
  逐项过程见 git 历史 #3。

## 2026-11-16 · 文档规则收敛：守门员、登记表、脚本去重

- **改了什么**：
  - `ops/check-links.py` 重写为文档规则的**唯一守门员**：除原有 markdown 链接外，新增
    「反引号 / 代码注释 / 配置注释里的 `docs/NN...` 引用（含裸编号）必须存在」与
    「`docs/README.md` 登记表 ↔ 实际编号文档一一对应」；扫描**工作区**而非 `git ls-files`，
    新建但还没 `git add` 的文档同样被检查。
  - 新增 `ops/test-check-links.py`（6/6 护栏自检，故意写坏文档确认规则不永远绿），CI lint job 加一步。
  - 删除 `ops/link-check.py` 里重复的「文档内相对链接」检查段：云端没有 docs 只会 SKIP，
    本地又与 `check-links.py` 跑同一件事、判定还不一致。联动体检 46 → 45，当前口径数字同步。
  - 全仓清理 **31 处失效引用**：AGENTS §5/§6、`nonebot/app_core` 十个模块的注释、
    `config.example.yaml`、ops 多个脚本与 CI 注释里的旧编号，统一指向 5 篇现行文档。
  - 规则收敛：`docs/README.md` 新增「规则（唯一真相源）」；AGENTS §1/§5 与 CONTRIBUTING §2/§5
    改为速查与指针，不再复制规则正文；澄清 `link-check.py`（联动）与 `check-links.py`（文档）分工。
- **为什么**：19 篇合并为 5 篇后，AGENTS 与代码注释里仍有十几处 `docs/00`、`docs/06`、
  `docs/11`、`docs/17` 指向已删文件，旧检查只查 `[xxx](yyy)` 一处都抓不到；
  规则又散在四处并已开始漂移。新守门员首跑列出 31 项，修完归零。
- **验证**：`check-links.py` 21 篇 md / 92 链接 / 80 处路径引用 / 5 项登记 PASS；
  `test-check-links.py` 6/6；`lint-plugins` FAIL 0 / WARN 0；`lint-layers` PASS；`test-guards` 4/4。
- **部署**：`ops/` 脚本行为有变更（联动体检少一项重复检查），`nonebot/` 下为纯注释；
  按约定需部署，`QQBOT_HOST` 仍缺，本次未执行。

## 2026-11-16 · 文档重构：19 篇合并为 5 篇分层叙述

- **改了什么**：`docs/` 从 19 篇编号文档（2214 行）压成 **5 篇 + 一张地图**：
  `01-定义与决策` / `02-系统与扩展` / `03-运维与通道` / `04-评测与实验` / `05-改动台账`。
  删除 17 篇旧文档；全仓引用（README / README.en / AGENTS / CONTRIBUTING / 插件模板 /
  代码注释）迁移到新路径。上一条新建的 `docs/22`、`docs/21` 一并合并重命名，
  旧内容可在 git 历史 `5662596` 找回；本文件里更早的历史条目保留旧路径不改（流水记录）。
- **为什么**：文档数量与重复同步成本已超过收益，且状态标注互相矛盾——
  `docs/18` 标「设计稿，未开始跑」却已有多轮实测；`docs/19` 的迁移路径未落地；
  新旧定义并存。每加一个改动要在多篇里同步，反而没人读得完。
- **不只是搬家**：03 删掉已失效的 SnowLuma / NapCat 迁移清单（两家 11-09/10 停止维护），
  改为「自选仍在维护的 OneBot v11 实现并自行验证」；04 修正状态并把 6 组实测结论收成一张表；
  02 明确 45 秒 / 会话锁是实现细节、「补审计层后放开可授权短任务」（对齐 A+B+C）；
  影子实验集中到 04 并逐条标截止日（BM25 仍为 2026-11-19）。
- **验证**：`check-links.py` **23 篇 md / 92 条链接 PASS**（35 篇 → 23 篇）；
  `lint-plugins` FAIL 0 / WARN 0；`lint-layers` PASS；`test-guards` 4/4。
  逐文件改动见 git 历史 #1。
- **部署**：不适用——纯文档与注释改动，无 `nonebot/` / `ops/` 运行时代码，不重启生产。

## 2026-11-16 · 重新定义：可携带、可授权的持续陪伴主体（A+B+C）

- **改了什么**：新增 `docs/22-项目定义.md`（现行定义）与 `docs/21-改动台账.md`（逐项改动记录）；
  `docs/09` 加归档说明（不改名，避免破坏既有引用）；同步 `README.md` / `README.en.md` /
  `AGENTS.md` §8 与入口描述 / `docs/00` 定位、设计理念、决策表 / `docs/10` 两条注记 /
  `docs/README.md` 登记表与阅读路径、`pyproject.toml` 包描述。判据从四条换成五条：
  连续性、不可替代性、可携带性、可审计、表达不污染事实。
- **为什么改**（AGENTS.md §5 要求写明）：旧定义 2026-11-11 定「记忆是唯一主链路、对话只是端口」，
  12 天内定位被重写至少 5 次，且 11-15/16 的提交（语音、画图、飞书、副产物自知）全在表达与端口层；
  RealCompanion（arXiv 2610.01780）实测真实陪伴流量仅 3.4%（严格 1.3%）消息需要回忆、
  池化增益 96% 落在不需要记忆的探针、检索门控 AUROC 0.530——瓶颈是「何时不检索」而非「怎么检索」；
  AstrBot / Mem0 / Zep / memU 已把记忆做成可插拔基础设施。旧定义把可替换零件当本体、
  把不可替代的分寸当端口，并让每次演进都要先写辩护状。
  用户 2026-11-16 圈定方向 **A 关系连续性/克制 + B 可携带的「人」 + C 可授权地做事**，不做 D。
- **对既有决策的影响**：`docs/03` 删除 agentd 时「先有审计层」的前提改为「补审计层后放开可授权短任务」；
  「不遗忘」改为「不静默丢失，归档可查」；「单用户」改为「一个主体、多类关系」（不做多租户）；
  45 秒硬线与单进程降级为实现细节，不写进定义。
- **验证**：`check-links.py` 35 篇 md / 119 条链接 PASS；`lint-plugins` FAIL 0 / WARN 0；
  `lint-layers` PASS；`test-guards` 4/4（离线回归本地缺依赖未跑，靠云端 verify）。
  逐文件改动见 `docs/21-改动台账.md` #0。
- **部署**：本次仅仓库文档变更，`ops/deploy.sh` 只上传 `nonebot/` 与 `ops/`，
  无运行时代码改动、无需重启；后续代码改动按批次 commit 后部署并记录 `.deployed_sha`。

## 2026-11-15 · 飞书通道上线：长连接 + 私聊 + 文字/图片/语音

- 飞书自建应用「大肥鱼」跑通最小回环后接进内核：`plugins/feishu` 用 SDK 长连接
  （不需公网 IP/域名/内网穿透）收私聊事件，3 秒内 ack、后台交给 `TurnRunner`，
  和 QQ 共用同一套 45 秒硬线/分段/会话锁/记忆——主人 session 用
  `config.owner_session()`，换端也记得住。
- 只认 `config.feishu.owner_open_id`（可用范围设「仅自己」），群消息/陌生人直接忽略。
- `adapters/feishu.py`：把飞书 content JSON（text/post/image/audio/…）归一成
  `InboundMessage`，纯函数可离线测（新增 6 条断言）。
- 语音双向：出站 TTS silk → pcm → opus（飞书要求 `msg_type=audio` + `file_type=opus`），
  入站下载资源 → ffmpeg 转 wav → 复用现有 ASR；失败退回文字，不静默丢。
- 依赖：`lark-oapi==1.7.5`（要求 websockets<16）→ `websockets` 17.4→15.0.1
  （nonebot2 要求 >=15，取交集）；已进 `requirements.txt`，生产 venv 实测 `pip check` 干净、
  重启后 QQ 通道正常。
- 探针保留为运维工具：`ops/feishu-probe.py`（`--check` / 常驻收消息）。

## 2026-11-15 · 语音链路：TTS→PCM→silk，换全家桶，真的发出去

- 换家底座 + 429 退避：智谱高峰连续 429，会让「写记忆失败、水位不动」和「voice 空
  转写」——一次失败就放弃太脆。`memory_embed.embed` 与 `asr.transcribe` 对 429/5xx
  退避 3 次（1.5s 递增）；新增 `embed_provider.py` 把 api/model/维度/重试收一处，
  内置 zhipu(2048) / siliconflow(1024)，换家只改配置；维度不符直接抛，而不是召回
  静默退化成 recent。721/721、memory-port 37/37、39/39、lint PASS。
- TTS 出 silk：智谱 `/audio/speech`（cogtts）返回 PCM 24k mono，`pilk.encode` 写
  silk。实测 text → silk 7861 字节 → 解码 3.4 秒 24000Hz 单声道。失败返回 None
  不抛（一条语音合成失败不该让整轮崩），429/5xx 退避 2 次。
- ASR/TTS 换到 DeepSeek 全家桶：智谱欠费那晚语音和记忆一起挂，而 DeepSeek 的
  `/v1/tools/asr` 与 `/v1/tools/tts` 格式正好兼容。ASR 用 multipart 传 wav、返回
  `{text, duration_ms}`（原 `{text, error}` 契约不变）；TTS `format=pcm` 直接拿
  16-bit mono 24k，`pilk.encode` 不用改。闭环实测：TTS「明天下午三点开会，别迟到。」
  → 2.6s silk → ASR 转回一字不差；同句 2.6s vs 智谱 3.3s。
- 发语音：只做两个触发、不做概率策略（实测语音比文字更容易让人烦）——他这条是
  语音（`[语音]` 前缀）就回语音，他明确说「用语音」「读给我听」也回。
  `Channel.send_voice` 原语 → QQ 用 `MessageSegment.record(bytes)`（和表情包同理，
  不走 file://）；后台合成分段只读最后一段（几段拼起来像播报）；失败静默——文字
  已经发出去了，语音是补充不是替代。

## 2026-11-12 · 接住：允许问 / 允许不接 / 此刻状态

- `CATCHING_RULE`：状态不明先问「怎么了」「你说的是哪件」；「算了/行吧」可以只回一两个字，甚至不回。
- 新增 `app_core/situation.py`：最近在聊 / 还挂着 / 此刻情绪，每轮注入 `[此刻]`——
  这是**状态**（今天在弄什么），不是事实（会什么）。
- test-dialog 14/14；smoke 247；云端 5/5。

## 2026-11-10 · 移除 agentd

- 停用并删除 agentd 服务、sandbox/proxy 容器、自然语言任务入口和 `/task` 插件。
- 代码/数据/服务单元归档到 `/opt/backup/agentd-removed-20261110-201219.tgz` 与
  `/opt/backup/agentd-db-20261110-201219.db`；Git 回退点 `pre-agentd-removal-20261110`。
- 保留聊天、记忆、情绪、主动消息、OneBot 健康检查；验证改为五件套。
- 646 离线 / 239 全链路 / 48 面板；云端 5/5。

## 2026-11-10 · 引擎抽离 M1：Engine 门面

- 新增 app_core/engine.py：InboundMessage / OutboundMessage / Engine.handle；
  现在只包 ChatCore.reply，不搬业务代码。
- QQ 插件改走 Engine，行为不变；旧 core.reply 仍是实现，可随时回退。
- 新增 ops/test-engine.py（6 项）并接入 CI；全链路加 engine 覆盖。
- 651 离线 / 238 全链路 / 81 agentd / 48 面板；云端 6/6。

## 2026-10-26 · 初版

- NoneBot2 + OneBot v11 的聊天机器人：记忆、情绪、知识库、主动消息
