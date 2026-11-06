# agentd：能干活的 agent

> 4G 机器上的**单 agent、多步、可恢复**运行时。QQ 是它的窗口之一。
> 它和陪伴机器人**并行**：机器人聊天，它办事，两者不共享记忆。

---

## 一、边界

| 做 | 不做 |
|---|---|
| 单 agent、多步（计划 3–8 步，上限 30 步） | 多 agent 并行 |
| 沙箱执行 + 白名单出网 | 浏览器自动化 |
| 崩溃恢复、重规划、批量提问 | 本地模型 |
| 预算硬顶（步数/token/时间） | 多用户并发 |
| 危险命令审批 | 公网 Web（只监听回环） |

---

## 二、组件

```
QQ → plugins/agent（2 秒轮询发件箱）→ HTTP 127.0.0.1:8100 → agentd
                                                              │
   ┌──────────────────────────────────────────────────────────┤
   │ channel.py  /task /tasks /answer /approve /outbox /agent_usage /（看板）
   │ loop.py     队列 + 执行循环 + 恢复 + 重规划
   │ planner.py  出计划（noop 拦截）
   │ context.py  拼上下文 + 滚动摘要
   │ verify.py   验收 DSL
   │ tools.py    shell/文件/搜索/抓页/记忆/审批
   │ sandbox.py  docker exec + 生命周期 + OOM 判定
   │ llm.py      DeepSeek 调用 + 记账
   │ store.py    SQLite（7 张表）
   └───────────┬──────────────────────────┬───────────────────
               │ docker exec              │ HTTPS
      agentd-sandbox（512MB，按需）      DeepSeek API
               │ 唯一出口
      agentd-proxy:8888（白名单）
```

---

## 三、数据（`/opt/agentd/data/agentd.db`）

| 表 | 作用 |
|---|---|
| `tasks` | goal / project / status / tokens_used / deadline / report / extra |
| `steps` | idx / desc / status / depends_on / verify / attempts / result / error |
| `events` | 追加式日志：plan/act/observe/verify/error/note/answer/compact |
| `checkpoints` | 步边界快照（计划+文件 sha+摘要+预算） |
| `llm_calls` | 所有模型调用（预算按这张表算） |
| `outbox` | 待发消息（插件拉取） |
| `approvals` | 危险命令待批 |

状态机：`pending → running → done | needs_input | failed | cancelled | paused`

---

## 四、循环

```
取任务（FIFO，同时 1 个，队列上限 3）
  ├─ 新任务 → 规划（3–8 步）→ 写 PLAN.md
  ├─ 恢复 → 重置中断步骤 → 文件变了就重锚定
  └─ 循环：
       预算检查（步数/token/时间/日限额）
       取可执行步骤（依赖全 done）
       单步限时 = 剩余时间
       ├─ 拼上下文 → LLM 决策 → 解析工具调用
       ├─ 危险命令？→ 转审批，停
       ├─ 执行
       ├─ 验收（exit_code / file_exists / contains / regex / custom）
       ├─ 通过 → done + 检查点 + 滚动摘要
       ├─ 失败 → 分类处理
       └─ 心跳（超阈值且没满额度）
     全卡住 → 重规划一次
 收尾 → 汇总报告 + 写项目 STATE.md + 发 QQ
```

---

## 五、四类失败

| 类型 | 判定 | 处理 |
|---|---|---|
| 瞬时 | exit 124/137（非 OOM） | 退避重试 2 次 |
| 资源 | 137 + `OOMKilled=true` | **不重试**，降规模一次，再失败 blocked |
| 阻塞 | Permission denied / 文件不存在 / 路径越界 | 不重试，blocked |
| 逻辑 | 命令错、断言失败 | 换办法重试 ≤3 次 |

---

## 六、恢复

| 场景 | 做法 |
|---|---|
| 进程被 kill | 启动时把 `running` 步骤重置为 `pending`，从断点继续 |
| 断点可能已产生副作用 | 若该步有 `act` 事件，插「可能已执行过，先确认现状」 |
| 暂停期间文件变了 | 重锚定：新文件列表 + 旧摘要 → 新摘要 |
| 用户回答后 | 作废卡住的步骤，清「已重规划」，带答复重新规划 |
| 等太久 | `needs_input` 超 24 小时自动关闭 |

---

## 七、验收 DSL

每步必须带一个，否则只算「模型认为完成了」。

```json
{"exit_code": 0}
{"file_exists": "report.md", "min_size": 200}
{"contains": "异常 12 条"}
{"regex": "\\d+ 条"}
{"custom": "python check.py"}     ← 真的在沙箱里执行
{"noop": true}
```

- `custom` 在**沙箱**里跑（在宿主跑等于白送逃逸）
- `noop` 比例 >40% 会让 planner 重写一次
- 文件存在性查**真实文件**，兼容 `a.txt` / `/work/a.txt` / `/work/<id>/a.txt`

---

## 八、工具

| 工具 | 在哪执行 | 约束 |
|---|---|---|
| `shell` | 沙箱 | 只能访问白名单；超时 30s；输出 >4KB 落盘 |
| `read/write/list` | 宿主（限 `/work/<id>`） | realpath 校验，防 `../` 逃逸 |
| `grep_file` / `tail` | 宿主 | ≤50 行 |
| `search` | 宿主 | DeepSeek Search API，**不经代理** |
| `fetch` | 宿主 | **SSRF 防护**：只允许公网 80/443，内网/回环/云元数据全拒；重定向逐跳校验；剥隐形字符 + 划界 |
| `memory_*` | 宿主 | 启动时探测，连不上就**从工具表摘掉** |

**注意**：`shell` 里没有外网（只有包源白名单）。抓网页/搜索必须用 `fetch`/`search`。

---

## 九、通道

- **QQ**：`plugins/agent` 每 2 秒拉 `/outbox?since=<游标>`；游标**落盘**（重启不重放）
- **文件进出（QQ）**：
  - 进来：直接把文件发给机器人 → 落到 `/opt/agentd/inbox/`，30 分钟内有效；
    下一条 `/task` 自动带上（最多 5 个）
  - 出去：任务结束把产物（docx/pptx/xlsx/pdf/csv/md/txt/png/zip，≤20MB，最多 3 个）
    用 `upload_private_file` 传回；传不动就回一条 scp 路径
  - 原件不回传（除非被改过）
- **Web**：`127.0.0.1:8100/`，只读看板，5 秒自刷
- **消息节奏（默认只发结果）**：20 秒内结束的任务全程静默，只发一条结果
  （最后一步的结论 + 产物文件名）；超过 20 秒发一条「还在跑」，3 分钟后再补一条，
  全程最多 2 条过程消息。需要你回答、需要审批时才中断。

| 命令 | 作用 |
|---|---|
| `/task 目标` | 建任务 |
| `/tasks` | 最近任务 |
| `/progress 号` | 报告 |
| `/answer 号 答复` | 回答卡住的任务 |
| `/approve 号` | 批准危险命令 |
| `/agent_usage` | 今日 token 分布（`/usage` 是机器人的账，别撞名） |
| `/cancel` `/pause` `/resume` + 号 | 取消 / 暂停 / 继续 |

---

## 十、预算

| 层级 | 上限 | 触发 |
|---|---|---|
| 单步 | 剩余任务时间 | 该步 blocked |
| 单任务 | 30 步 / 15 万 token / 20 分钟 | 停，给已完成部分 |
| 单日 | 50 万 token | 新任务直接拒；>80% 发 QQ 提醒 |

记账口径是 `llm_calls`（含 plan/compact/reanchor），不是步数。

---

## 十一、安全

| 面 | 措施 |
|---|---|
| 执行 | docker，只读 rootfs，`--cap-drop ALL`，`--pids-limit 128` |
| 文件 | 只挂 `/work`（rw），宿主侧 realpath 校验 |
| 出网 | 内网（无路由）+ 代理白名单；iptables 禁直连宿主网关 |
| 凭证 | 沙箱内**没有任何 key** |
| 审批 | 按 argv 解析判断：`rm` 的递归/强制、`dd if=/of=`、`mkfs*`、`shutdown` 等；支持 `sudo` 前缀与 `;` `&&` 串联；子串匹配只作兜底 |
| OOM | QQ 容器 `-500`、沙箱 `+500`（宁可杀沙箱） |
| 审计 | 所有动作写 `events` |

**黑名单不是安全边界，只是「转审批的触发器」**——它决定「要不要问人」，不决定「能不能执行」。
名单永远有绕过方式（变量展开、编码、别名、脚本），所以**不要因为它看起来够全就放松沙箱**：
真正的边界只有三条——只有 `/work` 可写、沙箱内没有任何凭证、出网走白名单代理。

**uid**：沙箱内以 root 运行，隔离靠挂载面 + cap-drop，不靠用户隔离。

---

## 十二、运维

```bash
systemctl status agentd
curl 127.0.0.1:8100/health
journalctl -u agentd -f

# 沙箱出网（幂等）
sh agentd/sandbox/setup-net.sh

# OOM 优先级 + 出网隔离（已挂 timer，每 5 分钟校准）
systemctl start agentd-guard.service
```

沙箱按需启动，空闲 10 分钟自动停（冷启动 1–2 秒）。
备份已包含 `agentd.db`、`PLAN.md`、`report.md`、`projects/*/STATE.md`。

---

## 十三、验证

```
agentd/tests/test_core.py     81 项（假 LLM + 假沙箱，不联网不碰 docker）
```

覆盖：验收 DSL / 路径逃逸 / 溢出落盘 / 失败分类 / noop 拦截 / 端到端 /
阻塞传播 / 预算 / 重锚定 / 审批 / 崩溃恢复 / 重规划 / 截断 JSON 修复 / 等待超时 / 沙箱判定

**真机验证过的**：多步任务、暂停恢复、kill -9 恢复、危险命令审批、批量提问、
白名单出网（pip 9.8s）、沙箱无法触达宿主端口。

---

## 十四、已知边界

| 项 | 状态 |
|---|---|
| 沙箱环境复原 | 不做（pip 10 秒能装） |
| Web 鉴权 | 不做（只监听回环） |
| 重规划次数 | 每任务 1 次 |
| 队列 | 单队列 FIFO，不抢占（可 `/pause`） |
| 记忆 | 云端不可用时工具被摘除 |
