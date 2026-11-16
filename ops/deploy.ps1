# deploy.ps1 —— Windows 无 bash 时的等价部署入口，门禁顺序与 ops/deploy.sh 相同。
#
# 为什么需要：本地没有 bash（where bash 为空），而 deploy.sh 是唯一的部署入口。
# 这个脚本只做「等价搬运」，不改变门禁顺序：快照 → 上传 → 权限/版本号 →
# verify.sh --pre 过了才重启 → 全量 verify。
#
# 用法：
#   $env:QQBOT_HOST = "ubuntu@150.158.90.134"
#   $env:QQBOT_KEY  = "C:\Users\59911\Documents\Projects\qq-bot-v2\qq_bot.pem"
#   powershell -File ops/deploy.ps1
#
#   powershell -File ops/deploy.ps1 -NoRestart      # 只上传+验证
#   powershell -File ops/deploy.ps1 -SkipVerify     # 跳过重启前验证（紧急回滚用）
#   powershell -File ops/deploy.ps1 -NoPostVerify   # 重启后不跑全量五件套
#
# 密钥：只读本地 ssh 私钥；API 密钥仍在云端 /opt/qqbot-data/keys.json，本脚本不传、不覆盖。

param(
    [string]$SshHost = $env:QQBOT_HOST,
    [string]$Key = $env:QQBOT_KEY,
    [switch]$NoRestart,
    [switch]$SkipVerify,
    [switch]$NoPostVerify
)

$ErrorActionPreference = "Stop"
$Root = Split-Path $PSScriptRoot -Parent
Set-Location $Root

if (-not $SshHost) { Write-Host "缺少 -SshHost / QQBOT_HOST" -ForegroundColor Red; exit 2 }
if (-not $Key)     { Write-Host "缺少 -Key / QQBOT_KEY" -ForegroundColor Red; exit 2 }
if (-not (Test-Path $Key)) { Write-Host "找不到 ssh 私钥：$Key" -ForegroundColor Red; exit 2 }

$Sha = (git rev-parse --short HEAD).Trim()
$Ts  = Get-Date -Format "yyyyMMdd-HHmmss"
$Tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("qqbot-deploy-" + $Ts)
New-Item -ItemType Directory -Force -Path $Tmp | Out-Null

$SshOpts = @("-i", $Key, "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=20")

function Invoke-Remote([string]$Cmd) {
    & ssh @SshOpts $SshHost $Cmd
    if ($LASTEXITCODE -ne 0) { throw "远端命令失败（exit $LASTEXITCODE）：$Cmd" }
}

$dirty = (git status --porcelain)
if ($dirty) { Write-Host "WARN 工作区有未提交改动，部署的是 HEAD（$Sha），不是当前工作区文件" -ForegroundColor Yellow }

Write-Host "== 1/6 云端旧代码快照（回滚点）"
Invoke-Remote "sudo -n mkdir -p /opt/backup && sudo -n tar czf /opt/backup/code-$Ts.tgz -C /opt/nonebot app_core plugins adapters && sudo -n ls -l /opt/backup/code-$Ts.tgz"

Write-Host "== 2/6 上传 HEAD:$Sha 的 nonebot/ 与 ops/"
git archive --format=tar HEAD:nonebot -o "$Tmp\nonebot.tar"
if ($LASTEXITCODE -ne 0) { throw "git archive nonebot 失败" }
git archive --format=tar HEAD:ops -o "$Tmp\ops.tar"
if ($LASTEXITCODE -ne 0) { throw "git archive ops 失败" }

$remoteNg = ($SshHost + ":/tmp/nonebot-" + $Ts + ".tar")
$remoteOps = ($SshHost + ":/tmp/ops-" + $Ts + ".tar")
& scp @SshOpts "$Tmp\nonebot.tar" $remoteNg
if ($LASTEXITCODE -ne 0) { throw "scp nonebot.tar 失败" }
& scp @SshOpts "$Tmp\ops.tar" $remoteOps
if ($LASTEXITCODE -ne 0) { throw "scp ops.tar 失败" }

# 删除清单：tar 解压是覆盖写，不会删掉已从 HEAD 移除的文件（2026-11-16 删 retrieval.py
# 时云端残留旧文件，link-check 报「模块没进 smoke-all」才发现）。只删真实删除的文件。
# Invoke-Remote 返回行数组，先取第一行再 Trim（直接 .Trim() 会静默得到空值）
$prevSha = @(Invoke-Remote "cat /opt/nonebot/.deployed_sha 2>/dev/null")[0]
if ($prevSha) { $prevSha = $prevSha.Trim() }
if ($prevSha -and (git rev-parse --verify --quiet ($prevSha + "^{commit}"))) {
    $deleted = @(git diff --name-only --diff-filter=D $prevSha $Sha -- nonebot ops)
    foreach ($f in $deleted) {
        if ($f) { Invoke-Remote "rm -f /opt/nonebot/$f" }
    }
    if ($deleted.Count -gt 0) { Write-Host ("== 2.5/6 删除已移除的文件：" + ($deleted -join ", ")) }
} else {
    Write-Host "WARN 读不到上次部署 sha，跳过删除清单（云端可能残留已删文件）" -ForegroundColor Yellow
}
Invoke-Remote "tar -xf /tmp/nonebot-$Ts.tar -C /opt/nonebot && tar -xf /tmp/ops-$Ts.tar -C /opt/nonebot/ops && rm -f /tmp/nonebot-$Ts.tar /tmp/ops-$Ts.tar"

Write-Host "== 3/6 权限 / 缓存 / 部署版本号"
Invoke-Remote "sudo -n chown -R ubuntu:ubuntu /opt/nonebot/app_core /opt/nonebot/adapters /opt/nonebot/plugins /opt/nonebot/ops; find /opt/nonebot -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true; chmod +x /opt/nonebot/ops/*.sh; printf '%s\n' $Sha > /opt/nonebot/.deployed_sha; cat /opt/nonebot/.deployed_sha"

if (-not $SkipVerify) {
    Write-Host "== 4/6 重启前验证（--pre，不碰生产库、不发消息）"
    & ssh @SshOpts $SshHost "sudo -n /opt/nonebot/ops/verify.sh --pre"
    if ($LASTEXITCODE -ne 0) {
        Write-Host ""
        Write-Host "!! verify --pre 失败：没有重启，生产仍在跑旧代码。" -ForegroundColor Red
        Write-Host "!! 回滚代码：ssh $SshHost 'tar xzf /opt/backup/code-$Ts.tgz -C /opt/nonebot && systemctl restart nonebot'" -ForegroundColor Red
        Remove-Item -Recurse -Force $Tmp -ErrorAction SilentlyContinue
        exit 1
    }
}

if ($NoRestart) {
    Write-Host "== 完成：已上传并验证，未重启（-NoRestart）"
    Remove-Item -Recurse -Force $Tmp -ErrorAction SilentlyContinue
    exit 0
}

Write-Host "== 5/6 重启并确认存活"
Invoke-Remote "sudo -n systemctl restart nonebot; sleep 5; systemctl is-active nonebot"
Invoke-Remote "cat /opt/nonebot/.deployed_sha; systemctl is-active nonebot"

if (-not $NoPostVerify) {
    Write-Host "== 6/6 重启后全量验证（五件套）"
    & ssh @SshOpts $SshHost "sudo -n /opt/nonebot/ops/verify.sh"
    if ($LASTEXITCODE -ne 0) {
        Write-Host "!! 全量验证有失败。服务已是新代码；先看失败项，必要时回滚：" -ForegroundColor Red
        Write-Host "!! ssh $SshHost 'tar xzf /opt/backup/code-$Ts.tgz -C /opt/nonebot && systemctl restart nonebot'" -ForegroundColor Red
        Remove-Item -Recurse -Force $Tmp -ErrorAction SilentlyContinue
        exit 1
    }
}

Write-Host "== 完成：HEAD $Sha 已部署；回滚点 /opt/backup/code-$Ts.tgz"
Remove-Item -Recurse -Force $Tmp -ErrorAction SilentlyContinue
