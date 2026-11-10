# 变更记录

只记里程碑。日常的小修在 git history 里。

## 2026-11-10 · M0：10 条真实对话结构重放

- 新增 ops/test-golden-m0.py：从生产 chat.db 取 10 条真实 user/assistant 对，
  用 fake LLM + 临时 QQBOT_ROOT 重放，断言分段非空、内容不丢、每轮写 2 条历史。
- fixture 含个人聊天内容，只放云端/本地，不进 Git；脚本无 fixture 时 SKIP。
- 本次 22/22 通过。

## 2026-11-10 · 引擎抽离 M4：投递契约

- 新增 app_core/delivery.py：Capability（平台硬限制）与 DeliveryPolicy（段长/段数/间隔/时限）分离。
- plan_delivery 默认与当前 QQ split_reply 一致；平台 max_text_chars 是硬截断。
- 新增 ops/test-delivery.py：10 条分段 golden + 平台上限，共 24/24。
- **M4.1**：ChatCore.reply 改用 plan_delivery；默认 policy 用 turn.seg_max，动态段长 18–32 不变。
  Engine.handle 新增可选 caps/delivery_policy，现由 console/后续适配器显式传，QQ 默认走原行为。
- **M4.2**：QQ 插件显式传 Capability(name="qq")；ConsoleChannel 传 console 能力
  （max_text_chars=2000 / max_parts=4）；QQ capability 对 10 条 golden 不改变分段。

## 2026-11-10 · 引擎抽离 M3：身份映射

- 新增 app_core/identity.py：identity_map(platform, external_id, kind, internal_id)。
- QQ 内部 ID 继续用 QQ 号，存量数据零迁移；console/其他平台生成 c-<uuid>。
- admin 新增 identities 列表；旧 admin.user_id 继续生效，_is_owner 同时认两套。
- 651 离线 / 239 全链路 / 81 agentd / 48 面板；identity 6/6、console e2e 9/9；云端 6/6。

## 2026-11-10 · 引擎抽离 M2：适配器边界

- 新增 adapters/fake.py（记录 OutboundMessage）与 adapters/console.py（stdout 发送）。
- 新增 ops/test-console.py（6 项）并接入 CI；只验证边界，不接真实 ChatCore/API。
- 生产 QQ 链路不变。
- **M2.1**：ops/test-console-e2e.py 用真实 ChatCore + fake LLM + 临时 QQBOT_ROOT 跑通
  console 输入 → 记忆/历史落库 → 回复；6/6，不 import nonebot。

## 2026-11-10 · 引擎抽离 M1：Engine 门面

- 新增 app_core/engine.py：InboundMessage / OutboundMessage / Engine.handle；
  现在只包 ChatCore.reply，不搬业务代码。
- QQ 插件改走 Engine，行为不变；旧 core.reply 仍是实现，可随时回退。
- 新增 ops/test-engine.py（6 项）并接入 CI；全链路加 engine 覆盖。
- 651 离线 / 238 全链路 / 81 agentd / 48 面板；云端 6/6。

## 2026-11-07 · few-shot A/B：去掉代码里的 5 条

- 19 条真实场景 × 3 组（0 条 / 5 条 / 3 条代码 few-shot），同一人格 22 对例子 + 尾部锚点。
- 结果：三组在表情/短推论/自嘲上都没有分析；5 条组平均更短（9.3 字），且第二轮出现一次空回复；
  0 条和 3 条更自然，自嘲场景 0 条更暖。
- 结论：人格已有 22 对例子，代码 5 条没有稳定收益，反而过拟合短句；移除，只保留尾部「😂 → ？」。
- 651 离线 / 237 全链路 / 81 agentd / 48 面板；云端 6/6。

## 2026-11-07 · 注入压缩：compact 背景 + 尾部锚点

- 新增 chat.context_format，默认 compact：记忆/笔记/技能/纠正/待办合成一个【背景】块，
  带 [记忆] / 笔记索引 来源标签，总预算 1200 字，超了先丢待办/纠正/技能/笔记。
- 知识索引从 1000 字压到 500 字，技能只留 1 条、纠正/待办各 2 条。
- model 模式在用户消息前加尾部锚点：先甩态度、日常 1–2 句、表情用一句反应接住，
  并附一条「😂 → ？」短例子；背景块标注「只在相关时用，别硬提」。
- legacy 分块注入完整保留；改 chat.context_format=legacy 可回到旧注入（通常同时 style_mode=rules）。
- 648 离线 / 237 全链路 / 81 agentd / 48 面板；云端 6/6。

## 2026-11-07 · 回复策略：模型优先（style_mode）

- 新增 chat.style_mode，默认 model：跳过 style_block / is_reaction_turn / 剪建议 / 软语气，
  只给人格、原则和 5 条 few-shot，让模型自己判断“接情绪还是展开”。
- rules 模式完整保留；回退只需把 chat.style_mode 改成 rules 并重启，不用回滚代码。
- 图片提示在 model 模式下不再预判求助/分享，由模型看图判断。
- 640 离线 / 237 全链路 / 81 agentd / 48 面板；云端 6/6。

## 2026-11-07 · 拟人修正：表情 / 短推论先接情绪

- 新增 is_reaction_turn：纯表情、贴图、短调侃、冲着你来的短推论，不再走「分析/建议」。
- 这类轮次关思考、跳过长度/形式/时效规则，只给一句「接住情绪/吐槽/反问」。
- 图片提示按场景分流：求助图保留图文比对；分享/表情图改成「别描述画面、别拆文字」。
- 纠正类（不对/错了/瞎编）不按调侃处理，仍要认错。
- 635 离线 / 237 全链路 / 81 agentd / 48 面板；云端六件套 6/6。

## 2026-11-06 · 重构第一批：P0 / P1 / P2.1

- P0：新增 ops/lint-layers.py（kernel/domain 依赖方向）与 characterization 基线；CI 接入。
- P1：app_core/kernel（Clock/Settings/db/types），7 项测试；暂不改变现有调用。
- P2.1：召回打分抽成 app_core/memory_score.py 纯函数，recall 只做取矩阵/取文档/touch。
- 回归：628 离线 / 225 全链路 / 81 agentd / 48 面板；云端六件套 6/6。
- **P2.2**：memory 拆成 memory_common / memory_store / memory_index / memory_embed / memory_score，
  memory.py 从 1000+ 行降到 228 行门面；Mixin 过渡，Memory 接口与 DB 测试补丁不变。
- **P3**：四段流水线（prepare/compose/generate/commit）搬进 chat_turn.py，chat.py 1464 → 984 行；
  动态读 chat.DB/REPLY_MAX_TOKENS，测试补丁与流式行为不变。
- **P4.1**：mood 拆成 mood_domain（纯规则）+ mood_store（状态/历史）+ 35 行门面；
  STATE/STATE_DIR/CHAT_DB 的测试补丁仍通过动态读取生效。
- **P4.2**：proactive 候选来源/归一化搬到 proactive_sources.py，插件只做频控、调度和发送；
  policy 仍是纯函数。
- **P4.3.1**：followup 拆成 followup_domain（纯信号/状态机）+ followup_store（SQLite），
  followup.py 33 行门面；DB 测试补丁仍生效。
- **P4.3.2**：remind/scheduler 实现搬进 scheduling/，顶层只留 8 行门面；
  DB/CLEANUP_LOG 动态读取，测试补丁不变。
- **终止**：P5（knowledge/integrations）和 P6（schema/settings）不做，P4 为重构终点。

## 2026-11-06 · 配置面板 v2：人格 + 巡检话题

- 面板新增人格编辑：`data/persona.json` 与 `data/persona-stranger.json`，system_prompt + 示例对话；
  示例对话必须成对，保存后发 `/reload_persona` 生效。
- 新增巡检话题编辑：`data/watchlist.json`，最多 8 个，免重启；同名话题保留 last/ts 基线，
  改名视为新话题，删除即不再巡检。
- 面板分「常用 / 高级·风险 / 人格 / 巡检话题」四页：常用 14 项，高级 53 项；
  15 项高风险（管理员、API/模型、记忆清理、群白名单、画像注入）保存前必须勾选确认。
- 配置面板的「未知键」补上 `llm.thinking_casual` / `chat.shadow_followup` / `chat.shadow_sample`：
  这三个键原来只在 ChatCore 里用 get 兜底，没进 DEFAULTS，UI 会误报未知。
- 测试 25 → 44 项；人格/话题都走备份 + diff + 干跑，内容没变不会重写文件。

## 2026-11-06 · 本地配置面板 v1

`ops/config-ui.py`：只绑 127.0.0.1 的配置编辑面板。按 DEFAULTS 自动生成分组表单，
显示当前值/默认值/说明，保存前校验、备份、出 diff；用 ruamel.yaml round-trip
保留 config.yaml 的注释和顺序，PyYAML 做不到这一点（会把注释全冲掉）。

- 未知键只读保留；非法值不落盘；备份保留最近 30 份。
- token 鉴权 + Host/Origin 校验；不碰 keys.json、不执行任意命令。
- v1 不做一键重启（保存后显示 `systemctl restart nonebot`），人格/watchlist 留 v2。
- 新增 `ops/test-config-ui.py`（25 项）并接入 CI。

## 2026-11-06 · 主动询问准入：只问最近两周、像他的待办

线上 30 条未关闭待办里，约一半是机器人自己的待办、他没回的问题、一次性日程和
给机器人自己的规则；读取侧又按「最老、没问过」优先，于是连续几天都在翻 10-31 的旧账。

- **写入**：`followup.add` 用 `askable()` 拦掉这四类；summary prompt 补反例。
- **读取**：`due_for_asking` 只要 14 天内、问过 <3 次、且过 `askable()`；
  排序改成「没问过优先 + 新的优先」。`pending` 同样过滤，聊天提示不再翻旧账。
- **关闭**：`已经答过了` 算完成；`忘了/算了/不弄了` 标 cancelled（不算完成），
  并且这轮 prompt 里明确「别提它」。
- **好奇清单**：过滤「还没提过/没讲过/一直没定」这类信息缺口。
- **措辞**：主动消息带上「约 N 天前」，超过 3 天不许说「刚刚」。

存量 30 条未关闭待办在备份后清空，从零开始。

## 2026-11-06 · 召回排序 golden 用例

固定语料 + 固定向量，断言 top-3 的**顺序**（重要性保底 > 新近 > 久远），
低于阈值的必须被挡掉。以后改召回打分（含 BM25 转正）时这条会先红——
是「拆 recall()」的前置条件，比先拆更划算。

## 2026-11-06 · memory 第一刀：抽出 vec.py

`_pack` / `_unpack` / `_cos` 从 memory.py 拆到 `app_core/vec.py`（纯函数，不碰数据库）。
目的是掐掉反向依赖：meme（表情包）和 kb（知识库）原来得 `from app_core.memory import _pack`，
现在直接从 vec 取。memory 内部仍用旧名（`pack as _pack`），调用点不动。

## 2026-11-06 · 评审三件：实体别名、BM25 判据、learn 拆分

- **实体别名表**：`DSH` / `DeepSeek Harness` / `harness 架构` 归成一个实体，落库时归并、
  `memgraph` 在**读侧**也归一——老数据不用迁移。线上实测 dsh 从 67 条记忆涨到 134 条。
- **BM25 影子的判据**：14 天 / 50 次；平均重合 ≥4.0 砍、≤3.0 转正、中间人工抽 20 条。
  `/mem_status` 直接显示样本数与建议，`docs/04` 记了截止日（2026-11-19）。
- **learn.py 986 → 786 行**：复盘/周报（调模型那一半）拆到 `review.py`，`learn` 保留同名入口；
  不调模型的写入规则仍留在 learn（那一半可离线测）。


## 2026-11-05 · 表情包发送前压缩

真实数据：包里 180 张有 **109 张超过 300KB**，最大 4.3MB。base64 之后最大约 5.7MB，
走 WebSocket 发图又慢又容易被拒。发送前统一压到长边 800 / JPEG 85：
实测 895KB 的 PNG → 99KB；gif/webp 不动（动画会丢，且本来就小）。

顺带 `/meme_stats` 暴露语义命中率（本次启动以来 语义/分类 次数）。

## 2026-11-05 · 表情包为什么一张都发不出去

排查「对话从不发表情」时发现两层都断了：

1. **发送层**：`MessageSegment.image(Path(...))` 会被适配器转成 `file:///opt/...` 宿主机路径，
   而 OneBot 实现跑在 docker 里（只挂了三个 `/app` 卷），看不到宿主目录 → `ENOENT`。
   改成传 `bytes`（适配器编码成 `base64://`），不再依赖挂载。
2. **调用层**：主人人格 458 字里零提"表情"，工具又按情绪强度条件注入，模型没有稳定预期。
   改成工具常驻 + 描述里写明使用场景；频率由「每轮最多一张 + 24h 去重」控制。

顺带：去重记录落盘（重启不再失忆）、`meta()` 扫描所有包、`find_ex()` 返回匹配方式与分数、
语义失败不再静默、`meme_stats` 显示向量数与 24h 发送数、阈值默认值统一。

## 2026-11-05 · 评审五条：WAL、命令解析、健康快照、时钟冒烟、周报对比

- `chat.db` 开 WAL + `synchronous=NORMAL`，所有 aiosqlite 连接加 15s busy timeout：
  同进程十几个短连接并发写不再可能 `database is locked`。
- 危险命令从纯子串匹配改成 argv 解析：`rm -r -f` / `rm --recursive --force` / `rm $FLAGS`、
  `sudo` 前缀、`;` `&&` 串联都能识别；引号里的字符串不再误判。
- 新增 `/mem_status`：记忆条数、最近召回、最近写入、知识页数、今日矛盾判定、影子体积。
- 新增 `ops/smoke-clock.py`：真实时钟冒烟，覆盖 mock 时间测不到的重启/跳变/晚跑场景。
- 周报「影子这周」加「与上周相比」：召回重合、二次召回命中率、自指率、连边覆盖率的差值。
- 顺带修：字符串形态的 `/opt/...` 路径（scheduler / agent / proactive / tools）也走 `paths.py`；
  lint 新增「运行时路径不许写死 /opt」。

## 2026-11-05 · 写明外部依赖风险

QQ 个人号没有官方机器人 API，接入依赖 OneBot v11 实现（第三方逆向协议）。README / SECURITY 现在明确写了：
可能随时断、换实现只改 `.env`、有封号风险、建议用小号；架构图里也标了「第三方实现 · 可替换」。

## 2026-11-05 · 路径可配置：QQBOT_ROOT

运行时路径原来散落在 20 个模块里写死 `/opt/...`，换安装目录要改十几处，漏一处就静默写回 `/opt`。
现在统一由 `app_core/paths.py` 从 `QQBOT_ROOT`（默认 `/opt`）派生，模块只引用 `_paths.DATA` 这类常量。
离线回归加了两条断言：改根后 DATA / KEYS / 记忆库都跟着变。

## 2026-11-05 · 开源走查：三个只有新机器才暴露的问题

在干净机器上从零 clone、装依赖、跑测试，暴露了三个云端永远看不到的问题：

- **`memory_schema.sql` 从没被程序加载**：云端库是 AstrBot 时代遗留的，新装的人
  `documents` / `memory_atoms` 表根本不存在，第一条记忆就崩。现在 `Memory.init()`
  先执行 schema（全部 `IF NOT EXISTS`，幂等）。
- **`config.load()` 不清旧错误**：配置文件不存在时，上一次的解析错误一直挂着，
  换回正常配置仍然报警。现在每次加载都先清空。
- **Windows 控制台 GBK**：测试打印 emoji 直接 `UnicodeEncodeError` 打断整轮。
  四个测试脚本在输出前统一 `reconfigure(encoding="utf-8")`。

顺带补了「路径约定」：运行时路径写死为 `/opt/...`，装别处要改常量（README 已说明）。

## 2026-11-05 · 开源整理

- 许可证：MIT
- 目录重排：`脚本/` → `ops/`（与部署路径一致），`插件模板/` → `templates/plugin/`
- 移除私人运维脚本与开发日记；测试里的真实 QQ 号改为占位值

## 2026-11-05 · 长期维护

- 异地备份机制（可插拔：rclone / coscli / 自定义命令），未配置时周报点名
- 每周恢复演练：真解压 + `integrity_check` + 关键表行数 + 代码抽查
- 月度维护：jsonl 截断、WAL checkpoint、VACUUM
- 健康检查扩到 agentd、未回复消息、演练状态

## 2026-11-05 · 矛盾检测

- 新记忆写入时检查是否推翻旧记忆；是真就把旧的标成 superseded（软删、可恢复）
- 四道闸：相似度门槛、必须给理由、每次上限、全部落盘可审计

## 2026-11-05 · 自我画像

- 六个固定字段 + 置信度 + 最后证据时间；120 天无新证据自动作废
- 区分「新信息/修正」与「只是复述」，复述不改写措辞
- 复盘提示里带上每条画像的年龄与上次的值

## 2026-11-04 · agentd 上线

- 多步任务（3–8 步，上限 30）、docker 沙箱、白名单出网、SSRF 防护
- 断点恢复、重规划、危险命令审批、预算硬顶

## 2026-11-03 · 测试与联动

- 四件套 → 六件套；新增联动体检（真跑插件 handler，断言调用与参数）
- 全链路体检：`app_core` 每个函数至少跑通一次

## 2026-10-26 · 初版

- NoneBot2 + OneBot v11 的聊天机器人：记忆、情绪、知识库、主动消息
