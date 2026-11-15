# plugins/feishu

飞书通道：长连接收私聊 → `app_core.runner`（和 QQ 共用会话锁/45 秒硬线/分段）。

- 身份：只认 `config.feishu.owner_open_id`；主人 session 用 `config.owner_session()`，
  和 QQ 共用一份记忆。
- 能力：文本、图片、语音（语音要 silk→pcm→opus 转一次；入站反之走 ASR）。
- 配置：`nonebot/config.yaml` 的 `feishu:` 段；App ID/Secret 放
  `/opt/qqbot-data/keys.json` 的 `feishu` 字段（不进仓库）。
- 不支持：群聊（只做私聊）、飞书「正在输入」（协议没有）。
