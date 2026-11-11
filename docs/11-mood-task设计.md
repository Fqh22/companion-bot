# mood / task 的契约设计

> 两类为什么不能直接塞进 `MemoryPort`；各自该用什么形状；迁移顺序与判据。
> 结论先说：**task 进 MemoryPort，mood 另立 AffectPort。**

## 一、现状

| 类型 | 存储 | 形状 |
|---|---|---|
| mood | `mood/state.json` | 单例状态：`valence`（τ=5h）、`valence_base`（τ=72h）、`energy`；撞界减速 |
| task | `followups` 表 | 条目：`text / status(todo·confirming·done·cancelled) / ask_count` |
| task | `reminders` 表 | 条目：`text / due_ts / sent` |

## 二、为什么不能都塞进 MemoryItem

`MemoryItem` 是**条目**语义：一条 text、一个 ref、可枚举、可检索、可删除。
适合它的是“多条、各自独立”的东西。

- **task 是条目**：可以进。
- **mood 是状态**：只有一个，没有 text，每轮读写，衰减是连续函数。
  塞进 `MemoryItem` 会得到“一条永远在变的条目”——列表语义失效，`forget` 也没意义。

所以两个接口，不是两个类型：

| 接口 | 语义 | 典型操作 |
|---|---|---|
| `MemoryPort` | 条目 | recall / remember / forget |
| `AffectPort` | 状态 | read / write / decay |

## 三、task 契约

**状态机**（权威是 `status` 列，`done` 由它派生）：

```
todo ──(问到回应)──> confirming ──> done
  │                      │
  └──(过期/没用)──> expired     └──(他取消)──> cancelled
```

**接口**：

| 操作 | 行为 |
|---|---|
| `recall(types=("task",))` | 未关闭的（todo / confirming），按 `due_ts` 升序 |
| `remember(task)` | 创建：text + 可选 `due_ts` |
| `forget(ref, type="task")` | **取消**（cancelled），不物理删——任务要留痕 |

**判据**：open 任务数、提醒送达率、`/remind_list` 输出与迁移前逐字一致。

## 四、mood 契约（AffectPort）

**接口**：

```python
class AffectPort(Protocol):
    def read(self) -> dict:              # {valence, valence_base, energy, updated_at}
    def write(self, state: dict) -> None:  # 全量写回（衰减由调用方算）
    def observe(self, text: str, vec=None) -> None:  # 按一句话更新（内部仍走 mood.py）
```

**为什么单独**：它不是“问句召回”，是“当前状态”。每轮必读，和查询无关；写有副作用（衰减），
不能像 `recall(touch=False)` 那样随便影子调用。

**判据**：同一输入序列下，`valence` / `energy` 曲线与迁移前一致（重放对比，容差 1e-6）。

## 五、与 MemoryPort 的关系

- `MemoryPort` 管“条目”：fact / profile / knowledge / **task**；
- `AffectPort` 管“状态”：mood。
- 两者都是记忆的形态，但**接口不合并**——合并会让 `recall` 的语义变成“什么都能查”。

## 六、迁移顺序

1. **task 读**：`recall` 加 task 分支（读 followups 未关闭 + reminders 未发送）→ 影子对比 `/remind_list` 输出；
2. **task 写**：`remember` 接创建、`forget` 接取消 → 测试状态机；
3. **mood 读**：定义 `AffectPort` + 适配器（读 state.json）→ 影子对比；
4. **mood 写**：`observe` / `write` 接过来；
5. 入口（主动消息用 task、情绪用 mood）最后迁。

每一步都是“纯移动 + 影子一致”，不做大爆炸。

## 七、风险

| 风险 | 缓解 |
|---|---|
| task 的 `done` 派生列与 `status` 冲突 | 只读 `status`，`done` 当兼容字段 |
| mood 的写有副作用，影子会污染 | 影子只调 `read`，不调 `observe` |
| 三套路径并存（Port / 旧 / 新） | 每类迁完立刻删旧路径，不留双轨 |
| `forget(task)` 被当成真删 | 语义写死：task 的 forget = 取消 |

## 八、判据（什么算成功）

- **task**：open 任务数不变、提醒送达不降、`/remind_list` 逐字一致；
- **mood**：重放曲线的最大偏差 < 1e-6；
- 两者都满足后，才把 `MEMORY_TYPES` 加到四类，并把 `UNIMPLEMENTED_TYPES` 减到 `("mood",)`（mood 走 AffectPort）。
