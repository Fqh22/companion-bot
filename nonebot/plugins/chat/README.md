# chat

QQ 消息接入层。宿主通过 `companion_core.shared_application()` 提供共用的应用实例。插件转换入站事件并回传送达凭据，历史与跟进由内核统一登记。

## 职责

| 能力 | 说明 |
|---|---|
| 消息合并 | 1.8 秒防抖，连发最多 6 条合并成一条处理 |
| 分段回复 | 1–2 句一组，间隔 1.2–2.8 秒 |
| 打字节奏 | 0.35–3 秒延迟，深夜/低精力更慢 |
| 文件 | 聊天里发文件自动解析，交 `app_core/files.py` |
| 表情包 | 按需附加发送，图片由 `core.reply()` 返回 |

## 关键约束

- **群聊默认关闭**：`GROUP_ENABLED = False`。
- 同一会话用 `_lock(sid)` 串行，防止并发 `reply` 打乱历史顺序。
- 业务逻辑全在 `app_core/chat.py`，这里只做事件转换。

## 配置

`config.yaml` 的 `llm` / `context` / `meme` 段。
