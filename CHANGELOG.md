# 变更记录

只记里程碑。日常的小修在 git history 里。

## 2026-11-11 · 全链路重放测试；修复画像注入缺 await

- `replay-diff.py` 改为**冒烟判据**：召回一致 + 无异常 + 无空回复；相似度只记录、不判死。
- 排除图片/文件样本（重放无法还原上下文）；临时目录用后清理；报告写 `data/replay_diff.json`。

- 新增 `ops/replay-diff.py`：复制生产库到临时根，取真实对话重放，比较旧回复与新回复
  （召回一致率 / 长度比 / 2-gram 相似度），写 `replay_diff.json`。
- **抓到真 bug**：`_prepare_profile` 改 async 后调用处缺 `await`，画像注入实际未生效
  （`RuntimeWarning: coroutine was never awaited`）。已修复并部署。
- 重放结果：召回 10/10 一致；平均相似度 0.07、长度比 0.84。相似度低是重放的固有噪声
  （LLM 随机 + 时间/情绪不同），**不作为回归判据**；判据仍是注入一致 + 647/241/43。
- 云端 verify 5/5。

## 2026-11-12 · 语音输入 P0/P1：转码 + 智谱转写 + 插件入口

- `app_core/asr.py`：ffmpeg 转 16k 单声道 wav、≤30 秒/25MB 检查、智谱 `glm-asr-2512` 转写。
- QQ 插件 `record` 分支：下载 → 转码 → 转写 → 文本进对话；失败回「没听清」，不猜内容。
- `test-asr` 在没有 ffmpeg 的机器上返回 2（未执行），不冒充通过。
- 云端 verify 5/5 + asr 4/4。

## 2026-11-12 · 语音输入方案（docs/13）

- 链路：record → 下载 → ffmpeg 16k wav → VAD → ASR → 场景标注 → 对话。
- 选型：**云 API 默认**（约 0.002–0.01 元/分钟，1–3 秒）；本地 faster-whisper 留开关
  （服务器无 GPU、3.7G 内存，只适合 base + 短语音）。
- 场景：人声 / 音乐 / 环境音 / 静音 / 多人；音乐不进对话，静音回「没听清」。
- 默认不保存原音频；P0 转码、P1 转写、P2 场景、P3 TTS（可选）。

## 2026-11-12 · 雷区清单；召回复查

- prompt 加 `LANDMINES`（负面约束）：安慰的雷（套话/急给方案/感叹号）、
  分析的雷（不给结论/把随口当课题/排比 bullet）、玩笑的雷（解释笑点/突然正经/上价值/连回三条）。
- 召回复查（`shadow-memory` 30 条）：一致 30/30、空结果 7%、**平均 4.57 条/轮**——
  仍接近上限 5，提示「凑满」可能，先观察不改阈值。
- 判断依据来自外部评审：没接住多半是「选料」问题，不是分类问题。

## 2026-11-12 · P1：回话自检标签上线（只观测）

- `SELF_CHECK_RULE`：模型正文前输出 `[自检] 主=… 次=… 置信=… | 依据=… 来源=…`；
  五类主：接住/判断/事实/执行/轻接；拿不准按接住。
- `app_core/self_check.py` 剥离标签；流式等换行再剥，避免半截标签发给用户。
- 记录 `data/self_check.jsonl`（tag / query / reply）；**回复本身不变**。
- 实测三条：判断准、来源能分记忆/搜索。test-self-check 6/6；647/244/43；云端 5/5。

## 2026-11-12 · 默认模型换成 chat；回话自检方案

- `llm.model` 默认 flash → **deepseek-chat**（A/B：判断/澄清/承认不知道更稳；编数据两者一样）。
- 云端 `config.yaml` 同步（备份 `.bak-model-*`），verify 5/5。
- 新增 `docs/12-回话自检.md`：用**一行自检标签**（他在要什么 / 有没有依据 / 依据从哪来）
  代替场景枚举；P1 只观测、P2 按标签路由搜索、P3 按标签措辞。

## 2026-11-12 · 模型 A/B：flash vs chat（同 5 条真实问题）

- 新增 `ops/ab-model.py`：临时根 + 复制库，只改 `llm.model`。
- 结果：
  - chat 明显更稳：Q1「我没听过这词」承认不知道；Q4 反问澄清；Q5 判断带理由。
  - flash 更容易自嘲/空转（Q1「缺口就在我这儿」、Q4「你方向是啥来着，我忘了」）。
  - **编数据没改善**：chat 的 Q3 仍是「查了下，脉脉 +789%、6.3 万」。
- 结论：换模型解决「判断 / 语气 / 承认不知道」，不解决「编造」；后者仍要来源约束或拦截。

## 2026-11-12 · 分析类意图：疑问形式 + 分析词

- `intent.looks_analytical`：疑问形式（？/吗/呢/怎么/什么/该不该/靠不靠谱…）**且**分析词
  （选/该/值/分析/对比/缺口/情况/区别…）才触发；`_prepare_search` 改用它。
- 语义阈值方案试过并放弃：30 条真实问句里，陈述句 0.55–0.62、真问题 0.51，分布太平；
  模块已删，标定数据留在 CHANGELOG。
- test-dialog 10/10；云端 5/5。

## 2026-11-11 · 分析类问题先搜一轮（给材料，而不是给规则）

- `chat_turn._prepare_search`：用户问句含「该不该 / 怎么办 / 形势 / 缺口 / 对比…」时，
  先 `web.search` 一次，结果作为「本轮搜索」注入 `extra_system`；`chat.force_search=false` 可关。
- 动机：实测 B 版（例子）更自然但**仍在编数据**——它觉得「这个我懂」就不搜，只能拿印象凑。
- 跑 C 版（B + 强制搜索）：Q3 的数据开始有出处；Q1/Q2 空是 replay 临时根无记忆，不代表真实。
- 647 / 243 / 43 通过；云端 5/5。

## 2026-11-11 · Prompt B 版：例子代替禁令

- `MODEL_FIRST_EXAMPLES` 加两个分析场景：判断+依据+下一步；承认没依据。
- `MODEL_FIRST_TAIL` 删掉三件套禁令，改一句「有依据说来源，没依据说没把握」。
- `FRESHNESS_RULE` 软化：不再「只能来自」，改为「没来源说清是印象」。
- `ops/ab-prompt.py`：同一批真实问题跑 A/B。
  结果：B 更口语（「问过一遍了呀」），但 Q3 **仍编数据**（「查了下，智联 24.8%」）——
  例子改语气不改事实；根治要靠给材料（搜索）+ fact_check 拦截。

## 2026-11-11 · 无来源断言：软规则 + 硬检测

- `FRESHNESS_RULE` 加：机构名 / 报告名 / 百分比 / 年份只能来自本轮搜索或记忆，
  没有来源就说「我印象里」，别当事实讲。
- 新增 `app_core/fact_check.py`：抽具体断言（百分比、年月、书名、机构+报告），
  检查是否在本轮注入内容里——**只记录不改回复**（`data/fact_check.jsonl`）。
- 先摸误报率再决定是否拦截：直接删或重写会误伤「一年 365 天」这类常识数字。
- test-fact-check 6/6；smoke 243；云端 5/5。

## 2026-11-11 · 待办只在聊到相关时注入（防翻旧账）

- `open_items` 从「每轮注入 3 条」改成「只注入与用户本轮有词重叠的」；
  `_topic_overlap` 用 2-gram + 停用词，避免「算了」「随便问问」触发旧账。
- 新增 `ops/test-dialog.py`（5 项）进 CI。
- 云端 5/5。

## 2026-11-11 · 搜索摘要清理 + 引用约束

- `web.clean_summary`：去 HTML/实体、标题重复、站点模板（来源/责任编辑，简繁）、开头时间戳。
  这些噪声会被模型当成正文，是幻觉的原料。
- `FRESHNESS_RULE` 加「搜了就标来源编号 [n]」。
- 新增 `ops/test-web.py`（7 项）进 CI。
- 实测同一 query：清理前摘要以「# 标题 来源：人民網222」开头，清理后直接是正文。

## 2026-11-11 · task 写入口接入；记忆使用周报

- 总结时的 followups 改走 `Port.remember(task)`（逐条，等价于 `add_many` 的循环）。
- 新增 `ops/memory-report.py`：读 `memory_trace.jsonl`，输出 0 条率 / 平均条数 / 类型分布。
- 首份报告：最近 7 轮，0 条 0%，**平均 5.0 条/轮**（贴着上限 5）——观察一周再决定是否调 `recall_min_score`。
- 647 / 242 / 43 / 34 / 5 全过；云端 5/5。

## 2026-11-11 · mood 进 AffectPort（五类骨架完成）

- 新增 `app_core/affect_port.py`：`read / observe / write`，纯转发 `mood.py`。
- `chat_turn` 的 load / observe / save 改走 Port；`chat.affect_port=false` 可回退。
- 新增 `ops/test-affect.py`（5 项）进 smoke；`test-affect` 5/5、smoke 242、云端 5/5。

## 2026-11-11 · task 写进契约：创建 + 取消

- `followup_store.id_for_text`：创建后拿到 id，契约能返回 `followup:<id>`。
- `Port.remember(task)` 只接 followup 创建；reminder 走 `remind_me`（要解析自然语言时间，不属于契约层）。
- `Port.forget(ref, type="task")`：`followup:<id>` → cancelled；`reminder:<id>` → 取消。
- port 34/34；云端 5/5。

## 2026-11-11 · task 读进契约（4/5）

- `recall(("task",))` 返回 followup 未关闭项 + reminder 未发送项，ref 为 `followup:12` / `reminder:3`。
- `MEMORY_TYPES` 加 `task`；`UNIMPLEMENTED_TYPES` 只剩 `mood`。
- 影子对比生产库：旧路径与新 Port **6/6 完全相同**。
- port 30/30；云端 5/5。

## 2026-11-11 · mood/task 契约设计稿

- 新增 `docs/11-mood-task设计.md`：结论 **task 进 MemoryPort，mood 另立 AffectPort**。
- 依据：`MemoryItem` 是条目语义（可枚举/可删），mood 是单例状态（连续衰减），塞进去会让 `recall` 失去意义。
- 含 task 状态机、AffectPort 接口、五步迁移顺序、四个风险与判据。

## 2026-11-11 · 外部评审修复：两处假绿、文档断档、3/5 如实标注

- `test-golden-m0.py`：缺 fixture 时 `exit 2`（未执行），不再打印「0/0 通过」。
- `scan-secrets.py`：没有可扫描文件时 `exit 2`，不再报「没发现问题」。
- 补 `docs/03-对话与工具.md`（原 `03-agentd` 删除后的空档）；README 索引。
- `docs/10` 工期改为「原估 11 工作日，实际当天完成」，写明偏差原因。
- `docs/09` 现状表加「契约覆盖 3/5」。
- `coverage-audit.py` 本机实测 18.1s（外审环境挂载慢，非脚本问题）。

## 2026-11-11 · 契约宽度 = 实现宽度（用户发现的静默缺口）

- `MEMORY_TYPES` 只声明**已实现**的三类（fact / profile / knowledge）；mood / task 移出，
  另立 `UNIMPLEMENTED_TYPES` 记录。
- `recall` 收到未实现类型直接 `NotImplementedError`，不再静默返回空列表
  （和 2026-11-05 `meme.find()` 吞异常是同一类毛病）。
- `forget(ref, type="fact")`：非 fact 显式报错，区分「没找到」和「这类型不支持」。
- 测试补 4 条（mood/task recall 报错、task forget 报错）。
- docs/09、docs/10 同步：五类表加「契约状态」列。
- port 29/29；云端 5/5。

## 2026-11-11 · 文档同步：MemoryPort 进模板 / 分层 / 联动矩阵 / 系统图

- `config.example.yaml` 加 `chat.memory_port`（排障开关，默认 true）。
- `docs/07` 的 orchestration 列表加 `memory_port`。
- `docs/04` 加 `/forget`、`/trace` 两行链路（谁在查、查什么）。
- `docs/01` 记忆闭环图改为 `MemoryPort.remember / recall`。
- `AGENTS.md` 红线加第 8 条：新代码只走 MemoryPort，不直接 import 存储模块。

## 2026-11-11 · P5 收尾：profile 写入走契约

- `learn.save_profile` 从 `apply_review` 抽出：纯文件写入、行为不变（合并 / 置信度 / seen / previous 规则只此一份）。
- `Port.remember_profile(fields, conf, evidence, changed)` 调它。
- port 26/26；云端 5/5。

## 2026-11-11 · P5：knowledge 写入走契约

- `Port.remember` 支持 knowledge → `learn.note_write`（payload：title / tag / project / source / stale_days）。
- `tools.note_write` 改走 Port，可开关回退。
- profile 写入**不硬塞**：它是 fields + conf + seen 三份数据，不是 `remember(text)` 的形状，
  需要单独设计，留 P5 剩余。
- 调度 / 主动消息入口依赖 profile 写入，继续留。
- port 24/24；云端 5/5。

## 2026-11-11 · P3 收尾：工具入口走契约

- `tools.memory_search` 改走 Port（fact 召回）；`read_full` 是全文读，不在 recall 契约，保持原样。
- 调度 / 主动消息入口**不迁**：它们写的是 profile / knowledge，契约的 `remember` 还没覆盖，
  迁过去等于假装接上。
- 云端 verify 5/5。

## 2026-11-11 · P3 收尾：profile 走契约 + 影子判断脚本

- `_prepare_profile` 改 async，画像文本走 Port（profile 类型）；fields 仍从旧路径取给影子记录。
- 影子对比：6 个字段 / 197 字，Port 与旧 `compact` 完全相同。
- `MemoryPort.recall` 加 `touch` 参数（影子用 `touch=False`，不污染访问时间）。
- 新增 `ops/shadow-memory.py`：真实消息对比新旧召回 + 空结果率，写 `data/memory_shadow.json`。
- 判据结果：30/30 一致、空结果 3%、平均 4.83 条 → 可继续。
- port 23/23；云端 5/5。

## 2026-11-11 · P4：记忆轨迹（/trace）

- 每轮 fact 召回后落一条 trace（ts / session / query / ids / 类型条数）到 `data/memory_trace.jsonl`。
- `introspect.trace_text` 读最近几轮；`/trace` 命令（QQ + registry）可查。
- port 23/23；云端 5/5。

## 2026-11-11 · P3：/forget 命令入口（两步确认）

- `introspect.forget_text`：关键词 → 列候选（前 8 位编号）；`/forget id:前缀` → 真删。
- `memory_store.find_by_prefix` + `CoreMemoryPort.resolve`。
- QQ 命令与 `default_registry` 各加一条（SUPERUSER）。
- 误删防护：不做关键词直删，必须两步。
- port 21/21；云端 5/5。

## 2026-11-11 · P3（部分）：forget 真删 + 知识读取走契约

- `memory_store.delete`：真删 documents / memory_atoms / memory_vectors / memory_ranges（逐表 try）。
- `CoreMemoryPort.forget`：用户要求忘记时真删，不走归档。
- knowledge 读取改走 Port（wiki 索引）；`kb.recall` 降级仍留旧路径。
- profile 读取未迁（需 async 改造）、`/forget` 命令入口未接，留 P3 剩余。
- port 18/18；云端 5/5。

## 2026-11-11 · P2：总结写入走契约

- `CoreMemoryPort.remember` 接 fact：`payload` 映射 session / topics / key_facts / vec / followups / range_key。
- `_summarize_once` 写记忆改走 Port；`chat.memory_port=false` 仍走旧路径。
- 双写校验用**参数级对比**：旧调用与 Port 调用的参数字典完全相等（不做物理双写，避免重复记一条）。
- `forget` 仍未实现（随 P3 清理逻辑一起做）。
- 云端 verify 5/5 + port 15/15。

## 2026-11-11 · P0/P1：记忆契约只读适配器

- 新增 `app_core/memory_port.py`：`MemoryItem` / `MemoryPort` / `CoreMemoryPort`（P1 只读）。
- `chat_turn` 的 fact 召回改走 Port；20 条真实消息对比旧路径，doc_id 列表 **20/20 一致**。
- `chat.memory_port` 开关（默认 true），false 退回旧路径。
- 新增 `ops/test-memory-port.py`（10 项）进 CI；smoke 覆盖。
- knowledge / profile 仍走旧路径，P3 再迁（依赖 query/qv/降级链）。
- 云端 verify 5/5 + port 10/10。

## 2026-11-11 · 重构方案：对话中心 → 记忆契约

- 新增 `docs/10-重构方案.md`：MemoryPort 契约、五类记忆（fact/profile/knowledge/mood/task）、
  五阶段（P0 契约 / P1 读 / P2 写 / P3 入口 / P4 explain）、每阶段验收与回滚。
- 硬约束：不改表、不迁数据、不换通道；P1 召回一致才继续 P2。
- 失败模式七条（召回变化/写入丢失/性能/类型混淆/抽象泄漏/万能层/半途迁移）与缓解。

## 2026-11-11 · 文案精简

- README 189 → 143 行：删掉与架构图重复的 ASCII 图、压缩「为什么 QQ 只是演示」与设计理念的论证。
- docs/09 精简到 55 行：只留定义、不变量、判据、差距、边界。

## 2026-11-11 · 设计初衷：它是一个记忆体，对话只是端口

- 新增 `docs/09-设计初衷.md`：定义、三个不变量、四条决策判据、现状差距、不做什么。
- README「设计理念」加链接；docs/00 定位加链接；AGENTS.md 新增「设计判据」一节。
- 判据：加东西前先答「读写什么记忆」；不是跨会话断言就不进记忆；拒绝万能层；拒绝表达污染事实。

## 2026-11-11 · 明确设计理念：似人，但不是人

- README 新增「设计理念」一节：保留情绪与连续性，不接受遗忘与失真；
  记忆层像数据库，表达层像人，两层不互相污染。
- docs/00 定位同步。

## 2026-11-11 · 门面：架构图与 README 示例

- `architecture.svg` 重画：内核居中（编排 / 领域 / 基础设施 / 数据），QQ 与 console 分列两侧；
  去掉 agentd 与「QQ companion bot」的独占表述。
- README 顶部加一段脱敏对话示例（短句接住、记忆延续、给判断不啰嗦）。
- README 引用架构图；「它坏了」改为「通道失效时替换适配层，内核不变」。
- README.en 同步。

## 2026-11-11 · 异地备份的替代方案：本地拉取

- 新增 `ops/sync-from-cloud.ps1`：本地开机时从云端补拉 `cloud-*.tgz`，
  `tar -tzf` 校验、本地保留 30 天、日志 `备份/sync.log`。
- Startup 的 `companion-backup.vbs` 登录时隐藏运行；不需要公网 IP。
- `备份/` 加入 .gitignore（30MB 级文件不入库）。
- 与云端 `offsite-backup.sh` 互补：配了 COS 是双保险，没配就靠本地。

## 2026-11-11 · 可靠性：清死服务、护住容器、告警兜底

- 删除 agentd-guard 残留：timer、service、`iptables -A INPUT -s 172.19.0.0/16 -d 172.19.0.1 -j DROP`、
  `agentd-net` 网络。备份在 `/opt/backup/agentd-guard-removed-*`（含 iptables 快照）。
- 新增 `snowluma-oom.timer`：每 5 分钟把 snowluma 的 `oom_score_adj` 拉回 -500
  （容器重启会丢，旧 guard 删掉后这项保护不能一起没）。
- 影子日志按 5MB 轮转（`shadow-profile` 11 天已 762KB，原来无上限）。
- healthcheck：QQ 告警发不出去时落盘 `alerts.jsonl`，恢复后补发——告警通道不再和被测链路同生共死。
- 云端 verify 5/5。

## 2026-11-11 · 规范化：入口文档 + 工具注册表 + 护栏自检 + 扩展指南

- 新增 `AGENTS.md`：先跑什么、五条扩展路径、红线、已定决策、AI 常见错误。
- `tools.execute` 从 13 个 `if name ==` 改成 `HANDLERS` 注册表；模块加载时自检 schema 与实现一致。
- lint 报错带修复指引（import nonebot / 硬编码路径 / domain 反向依赖 / adapters 越界）。
- 新增 `docs/08-扩展指南.md`：五条路径 + 检查命令 + 常见错误。
- 新增 `ops/test-guards.py`：故意写违规必须 FAIL（4 条），进 CI，防止规则悄悄失效。
- 21 套本地全绿；云端 verify 5/5 + guards 4/4。

## 2026-11-10 · 主动消息循环抽到内核；影子跟进关闭

- 新增 `app_core/proactive_runtime.py`：频控、回应率、话题冷却、生成、落库全在这里；
  `plugins/proactive` 只剩「发给哪个 bot」。
- `commands.default_registry` 增到 11 条（补 memory_cleanup / diary_write / review_now / weekly_now）。
- P2 决策：`chat.shadow_followup` 关闭（104 条样本、从未被消费）；
  BM25 影子保留到 11-19 判据日；实体别名保留（覆盖 99%，11-13 到期人工抽检）；
  矛盾检测与画像注入维持开启。
- 20 套全绿；云端 verify 5/5。

## 2026-11-10 · 分层收口 + 文档按「聊天内核」重写

- `ops/lint-layers.py` 新增两条规则：domain 不得 import orchestration；
  adapters 只走门面（不得 import chat / chat_turn / tool_loop）。反向验证过。
- 15 个模块的 docstring 首行加层标记（`[domain]` / `[orchestration]`）。
- 新增 `docs/07-分层.md`：三层职责、规则、新模块放哪层、为什么不物理搬家。
- README / README.en / docs/00 按「聊天内核」重写；01/02/04/05/06 统一术语与定位。
- 20 套本地全绿。

## 2026-11-10 · 命令分发与后台入口抽出

- 新增 `app_core/commands.py`：`/xxx 参数` 解析 + 注册表 + 权限；`default_registry(core)`
  自带 7 条只读命令（ping/help/debug/mood/usage/mem_status/profile）。
- 新增 `app_core/introspect.py`：只读命令的文本生成，QQ 插件与 console 共用，输出一致。
- 新增 `app_core/runtime.py`：`start_background(core, send)` 启动调度，不依赖 NoneBot。
- `InboundMessage` 补 `files` / `audio` 字段（为文件/语音通道预留）。
- `admin` / `commands` 插件改为调用 introspect（纯移动，行为不变）。
- 新增 `ops/test-commands.py`（11 项）；20 套本地全绿；云端 5/5。

## 2026-11-10 · 断线演练 + README 重写为「引擎优先」

- 新增 `ops/drill-console.py`：临时根起终端通道，不加载 NoneBot、不连 QQ，跑真实对话。
  实测两轮通过（「今天累死了」「这个报错又出来了」），证明没有 QQ 也能启动并说话。
- `ConsoleChannel` 处理 EOF（管道输入），演练可以脚本化。
- README / README.en 重写：定位从「QQ 陪伴机器人」改成「通道无关的陪伴引擎，QQ 是适配层」；
  补了接入通道的三原语、console 参考实现、QQ 为什么只是演示、数据与文档索引。
- 演练暴露的缺口（下一步）：命令分发、主动消息/定时入口、文件/语音入站、群聊、审批。

## 2026-11-10 · 抽出通道无关层：QQ 只是演示

- 新增 `app_core/runner.py`：`Channel` 三个原语（发文本/发图/typing）+ `TurnRunner`
  （typing 心跳、流式分段、45 秒硬线、补发尾巴）。
- `plugins/chat` 改用 `QQChannel` + `TurnRunner`，删掉约 300 行会话循环；行为不变。
- `ConsoleChannel` 升级成完整参考实现（流式 + typing），`run()` 走同一个 runner。
- 新增 `ops/test-runner.py`（7 项）并接入 CI；smoke-all 覆盖 runner。
- 新文档 `docs/06-移植指南.md`：分层、最小通道示例、两条迁移路线、数据迁移、能力清单。
- README / README.en 加「可移植性」一节。

## 2026-11-10 · 引用回复默认关

- 加 `chat.quote_reply`（默认 false）：第一段带引用框，实测每条都挂太重，先关掉。
- 开关保留：想开改成 true，只有第一段带；link-check 43 项（默认不引用 / 开启后引用各一条）。

## 2026-11-10 · 去掉「先分类再回答」的框架，加口语例子

- MODEL_FIRST_GUIDE 从「判断类型 → 按类回答」的流程，改成一句「像平时说话，别当客服」。
- MODEL_FIRST_EXAMPLES 从空恢复 4 组口语例子（累 / 报错 / 不弄了 / 名字怎么样）。
- replay：闲聊 7–8 字（「那就别撑着了」「贴出来，或者截图」）；
  制程题 52 字（「这个我不敢凭记忆报数……你是要装机选U，还是就看个热闹」）。
- 副作用：解释类会变长（2K 那条 193 字），思考常开 + 展开型问题叠加。

## 2026-11-10 · 引用回复 + 表情回应

- 回复的第一段带 `MessageSegment.reply`，引用他刚发的那条：从「悬空一句」变成「接着你说」。
  流式只有第一段引用；引用失败（实现不支持 / id 非法）自动退回纯文本。
- 他发表情/贴图/纯图时，extra_system 加一句「可以先用 meme_send 回一个」，让梗图对梗图。
  只在 reaction 轮加，正经问题不加。
- 联动体检 42 项（新增「第一段引用他的消息」）；云端 5/5。

## 2026-11-10 · 私聊显示「正在输入」

- 回复期间调 `set_input_status`（SnowLuma 实测支持），5 秒续一次，结束时关掉。
- 失败静默降级，不影响发送；群聊不调用。
- 这是整条链路里唯一一个「对方也在场」的可见信号——以前只有发送前的 sleep，用户什么都看不到。
- 验证：API 直连返回 retcode 0；云端 5/5。

## 2026-11-10 · 思考常开

- `llm.thinking_casual` 默认改成 false（代码默认、config.example.yaml、docs/01 同步）。短句、表情也不关思考。
- 分流逻辑保留在 `_thinking_for`：把配置改回 true 就恢复「短句、无问号、无技术词、不带图 → 关」。
- 代价：首字慢约 0.8 秒、每轮约多 100 token；647 离线 / 235 全链路；云端 5/5。

## 2026-11-10 · 关思考的阈值从 30 降到 20

A/B 实测（10 条真实消息，强制开/关对比）：开思考在「制程」「2K 是什么」这类问题上
明显更准——关思考那条先报了 N3/18A 又反悔「不敢凭印象」，开思考直接说「没搜到最新数，不瞎报」。
平均字数几乎一样（35 vs 38），所以「短」不是思考模式造成的。

但分流有漏洞：26–29 字的句子（「dsh 和 cc 都只是平台…」「还有什么和 harness 差不多价格好用的」）
被当成闲聊关了思考，它既不搜也不推，直接凭印象答。阈值降到 20，并把
价格/推荐/哪个好/值不值/合适/搜/找 加进特征词。

## 2026-11-10 · 把「怎么说」从规则改成信号

- MODEL_FIRST_TAIL 去掉「1–2 句、最多两个逗号、表情接一句、😂→？」——每轮的句数上限没了。
- 时效规则从 rules 模式的 style_block 提到稳定前缀：model 模式以前**根本拿不到**「知识会过时」。
- 时间统一放尾部，model / rules 都有；以前只有 rules 模式的 style_block 带时间。
- style_block 改成信号包：只给「他这条几个字 / 在问 / 在说状态 / 附图几张 / 最近平均」，判断交还模型；
  只有求助图保留一句保底。
- 纠正 22 → 3 条（只留可场景的：图片 / 长段 / 术语）；输出侧 3 条移进人格；每轮 lessons 从 2 条降到 1 条。
- 644 离线 / 235 全链路 / 41 联动 / 6 插件 / 48 面板；云端 5/5。

## 2026-11-10 · 减法：按 11 天真实使用删掉没人用的

数据（云端 chat.db 只读统计，11 天）：1889 条消息、927 条来自他；日志里 /usage 54 次、
/help 3 次，其余 25 条命令 0 次；主动消息 5 条（curious 3 / followup 1 / memory 1），
只有 followup 被回复；技能 0 个。

- 删技能三个工具（skill_save / skill_list / skill_read）与 /skill_list：
  0 产出，但工具 schema 每轮都进 prompt。learn 里的函数保留，手工放文件仍可用。
- /help 只留 11 条日常，其余 18 条收进 /debug；lint 仍校验全量命令注册。
- 主动消息只留 followup，去掉抽签和泛话题：没话找话对这个人无效（他画像里写着讨厌空话）。
- 删 kb_vectors 死表（13 行历史数据，代码零引用）。
- 646 离线 / 235 全链路 / 41 联动 / 6 插件 / 48 面板。

## 2026-11-10 · 审视后修复：测试隔离 / CI 守门 / 拆 prepare

- 测试和体检统一在 import 前把 `QQBOT_ROOT` 指到临时根。以前只逐个重定向 8 个模块，
  wiki / shadow / diary / meme / contradict / scheduler / watch 仍读默认根：本地会在
  `C:\opt` 留 chat.db，云端每次 `verify.sh` 都会覆盖 `/opt/nonebot/data/shadow-weekly.json`，
  下周周报的「上周对比」就失真了。新增「默认根已指向临时目录」断言（离线 647、全链路 240）。
- CI 补三套守门员：link-check（41）、smoke-plugins（6 插件）、smoke-clock（真实时钟）。
- `record_usage` 记账失败、proactive 建表失败不再静默吞异常，改为 warning。
- `chat_turn._turn_prepare` 144 行拆成四个子方法（followup / vectors / recall / knowledge / profile），纯移动。
- 清理 agentd 残留（测试临时子目录、注释），文档数字同步。

## 2026-11-10 · 移除 agentd

- 停用并删除 agentd 服务、sandbox/proxy 容器、自然语言任务入口和 `/task` 插件。
- 代码/数据/服务单元归档到 `/opt/backup/agentd-removed-20261110-201219.tgz` 与
  `/opt/backup/agentd-db-20261110-201219.db`；Git 回退点 `pre-agentd-removal-20261110`。
- 保留聊天、记忆、情绪、主动消息、OneBot 健康检查；验证改为五件套。
- 646 离线 / 239 全链路 / 48 面板；云端 5/5。

## 2026-11-10 · agentd v2：计划契约/状态机/产物

- 新增 plan.py：PlanStep/VerifySpec；空 verify 降级 noop，非法依赖丢弃，custom 只认已创建脚本。
- 新增 state.py：pending/running/needs_input/done/failed 显式迁移；回答走 needs_input -> pending。
- 新增 artifacts.py：收尾写 path/size/sha256 到 tasks.extra.artifacts，QQ 回传从清单来。
- agentd 92/92；不引入 LangGraph/Temporal/OpenHands，先按自家契约硬化。

## 2026-11-10 · SnowLuma 退路

- 备份当前 SnowLuma 镜像（698M）与配置；不重建当前容器。
- 新增 OneBot 健康检查：容器/NoneBot/3001 连续 2 次失败告警，支持 ALERT_WEBHOOK。
- 运维文档补 OneBot 后端替换清单；核心/agentd 不受 SnowLuma 影响。

## 2026-11-10 · 任务入口：自然语言 + 确认

- chat 新增任务意图：文件/动作命中后先问「要交给 agent 做吗？」，回「开始」才建任务；
  「交给 agent：…」可直接建；2 分钟确认过期；`/task` 保留。
- agentd 修输入预检：附件没搬进 /work 或目标引用附件但没带文件 → 直接 needs_input，不再空跑。
- planner 修无效 custom verify：引用未创建脚本自动降级成 exit_code。
- 测试：任务意图 12/12；agentd 92/92；全链路 243/243。

## 2026-11-10 · M6-mini：companion_core 门面

- 新增 nonebot/companion_core：EngineConfig（import 前设置 QQBOT_ROOT）+ create_engine 门面。
- 新增 pyproject.toml：可发现 app_core / adapters / companion_core 三个包。
- 新增 ops/test-package.py（5/5）并接入 CI；pyproject 可解析、配置注入生效。
- 当前仍是“门面 + 可安装包”，app_core 未物理搬家；M6-full 再做。

## 2026-11-10 · 引擎抽离 M5：入站/审批契约

- 新增 app_core/approval.py：ApprovalRequest / ApprovalResult / ApprovalPort，不绑定 QQ 或按钮。
- 新增 adapters/qq.py：OneBot 字段 -> InboundMessage（私聊/群聊/事件/引用/入库文本）。
- 测试：审批 5/5、入站 6/6；651 离线 / 241 全链路 / 81 agentd / 48 面板。
- **M5.1**：_handle 把 event_id / user_id / chat_id / reply_to 存进 Turn.meta，
  _run_turn 用 adapters/qq.to_inbound 构造 InboundMessage；adapters/ 移到 nonebot/adapters/
  以便 bot 进程能直接 import。仍是 QQ 行为。
- **M5.2**：ApprovalPort 增加 CommandApprovalPort（ask 挂起 / resolve 命令），
  审批测试 10/10；agentd 仍走自己的 HTTP approval，不强行改，等结构化审批再接线。

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
