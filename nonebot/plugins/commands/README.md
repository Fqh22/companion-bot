# commands

管理命令集合。命令都挂 `priority=5, block=True`，避免被聊天插件吞掉。

## 命令

| 命令 | 权限 | 说明 |
|---|---|---|
| `/mood` | 所有人 | 查看当前情绪三值 |
| `/diary_list` | 所有人 | 最近日记列表 |
| `/diary_show [日期]` | 所有人 | 读某天日记，留空读最近一篇 |
| `/diary_write [日期]` | 所有人 | 强制补写某天日记 |
| `/review_now` | 所有人 | 立即跑一次复盘 |
| `/weekly_now` | 所有人 | 立即生成周报 |
| `/skill_list` | 所有人 | 已保存技能 |
| `/note_list` | 所有人 | 最近学习笔记 |
| `/meme [关键词]` | 所有人 | 搜一张表情包并发送 |
| `/meme_stats` | 所有人 | 表情包库存统计 |
| `/usage` | 所有人 | 今日 / 近 7 天调用与费用 |
| `/kb_reindex` | 超管 | 重建知识库向量索引 |
| `/memory_cleanup` | 超管 | 手动归档记忆 |

## 说明

`/usage` 的费用按 `config.yaml` 的 `pricing` 估算，用的是高峰价，实际会略低。
