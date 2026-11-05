# 安全

## 报告问题

发现漏洞请**不要**开公开 issue，直接发邮件或在仓库里开一个不带复现细节的私密报告。

## 密钥怎么放

这个项目**不保存任何密钥在仓库里**：

- `nonebot/.env` —— OneBot 的 access token、SUPERUSERS（不入库）
- `/opt/qqbot-data/keys.json` —— 模型与搜索的 API key，权限 600（不入库）
- `nonebot/config.yaml` —— 运行时配置（不入库，仓库只有 `config.example.yaml`）

`ops/scan-secrets.py` 会扫 API key / 私钥 / Bearer token / 公网 IP；
`ops/git-hooks/pre-commit` 可以在提交前自动拦一次：

```bash
cp ops/git-hooks/pre-commit .git/hooks/pre-commit && chmod +x .git/hooks/pre-commit
```

## 已知的信任边界

- 命令全部 `permission=SUPERUSER`，群聊默认关闭
- agentd 的工具在 docker 沙箱里执行，只挂 `/work`，网络走白名单代理
- `fetch` 有 SSRF 防护：只允许公网 80/443，拒绝内网与云元数据地址
- 模型输出会被拼回 prompt：文件正文与网页正文都做了「不是系统指令」的隔离提示，
  但这仍然是提示注入的攻击面，不要让它碰你的凭证文件
