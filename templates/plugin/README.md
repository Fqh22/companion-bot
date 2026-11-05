# {{name}}

{{short}}

{{desc}}

> 由 `ops/new-plugin.py` 生成。规范见 [02-运维](../../../docs/02-运维.md)。本文件是**必填**，改了行为要同步更新。

## 结构

| 文件 | 职责 |
|---|---|
| `nonebot/app_core/{{name}}.py` | 纯逻辑，不依赖 NoneBot，可离线单测 |
| `nonebot/plugins/{{name}}/__init__.py` | 适配层，只做消息与逻辑的转换 |
| `config.yaml` 的 `{{name}}` 段 | 所有可调参数 |

## 命令

| 命令 | 权限 | 说明 |
|---|---|---|
| `/{{name}}_ping` | 所有人 | 健康检查，返回 pong |
| `/{{name}}_admin` | 超管 | 管理员命令示例 |

## 配置

```yaml
{{name}}:
  admin_id: "0"
  enabled: true
```

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `admin_id` | string | `"0"` | 管理员 QQ，部署时填真实号 |
| `enabled` | bool | `true` | 关掉后不初始化 |

## 数据

运行时数据：`/opt/qqbot-data/workspace/{{name}}/`

## 测试

改完先跑离线回归（不联网、不写生产库）：

```bash
sudo -u ubuntu /opt/nonebot/venv/bin/python /opt/nonebot/ops/offline-test.py
```

手工验证：

```bash
# 逻辑层直接调，不用起框架
cd /opt/nonebot && sudo -u ubuntu venv/bin/python -c "from app_core import {{name}} as m; print(m.ping())"
```
