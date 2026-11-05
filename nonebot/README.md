# nonebot/

机器人本体。完整文档在 [../docs/](../docs/)，这里只放代码。

```
app_core/   纯逻辑（不 import nonebot，可离线单测）
plugins/    适配层（只做消息与逻辑的转换）
config.example.yaml  运行配置模板（真实值在云端；本地要改就先复制成 config.yaml）
bot.py      入口
```

改完代码先跑离线回归（本地，不需要 QQ）：

```bash
export QQBOT_CODE_DIR=$PWD/nonebot
python ops/test-nonebot-offline.py
```

部署到服务器后对应的是 `sudo -u ubuntu /opt/nonebot/venv/bin/python /opt/nonebot/ops/offline-test.py`。
