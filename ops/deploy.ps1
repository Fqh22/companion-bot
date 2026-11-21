# deploy.ps1 —— Windows 无 bash 时的部署入口：构建 release → 候选验证 → 原子切换。
#
# 布局由 ops/release-switch.sh 维护：
#   /opt/nonebot-releases/<sha>-<ts>/   每个版本完整代码
#   /opt/nonebot/current -> 当前 release（切换只改这一个符号链接）
#   /opt/nonebot/{data,venv,config.yaml,.env} 保持真实，不随版本走
#   /opt/nonebot-ops-state/              ops 日志/state/ops.env 共享
#
# 顺序：快照 → 解到 release 目录 → release 内跑 verify --pre →
#       写版本标记 → 原子切换 current → 重启 → 全量 verify。
# 验证失败时生产目录、current 和版本标记都不动。
#
# 用法：
#   $env:QQBOT_HOST = "ubuntu@150.158.90.134"
#   $env:QQBOT_KEY  = "C:\Users\59911\Documents\Projects\qq-bot-v2\qq_bot.pem"
#   powershell -File ops/deploy.ps1
#   powershell -File ops/deploy.ps1 -NoRestart      # 只构建+验证，不切换
#   powershell -File ops/deploy.ps1 -SkipVerify     # 跳过候选验证（紧急回滚用）
#   powershell -File ops/deploy.ps1 -NoPostVerify   # 重启后不跑全量五件套

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
$RelId = "$Sha-$Ts"
$RelDir = "/opt/nonebot-releases/$RelId"
$Tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("qqbot-deploy-" + $Ts)
New-Item -ItemType Directory -Force -Path $Tmp | Out-Null
$SshOpts = @("-i", $Key, "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=20")

function Invoke-Remote([string]$Cmd) {
    & ssh @SshOpts $SshHost $Cmd
    if ($LASTEXITCODE -ne 0) { throw "远端命令失败（exit $LASTEXITCODE）：$Cmd" }
}

$dirty = (git status --porcelain)
if ($dirty) { Write-Host "WARN 工作区有未提交改动，部署的是 HEAD（$Sha），不是当前工作区文件" -ForegroundColor Yellow }

$migrated = (@(& ssh @SshOpts $SshHost "if [ -L /opt/nonebot/current ]; then echo yes; fi")[0] -eq "yes")
if ($migrated) {
    $Prev = (Invoke-Remote "basename `$(readlink /opt/nonebot/current)").Trim()
    $PrevRel = $Prev
} else {
    $Prev = (Invoke-Remote "cat /opt/nonebot/.deployed_sha 2>/dev/null").Trim()
    if ($Prev) { $PrevRel = "legacy-$Prev" } else { $PrevRel = "legacy-unknown" }
}
$Rollback = "ssh $SshHost 'sudo -n bash /opt/nonebot/ops/release-switch.sh rollback $PrevRel'"

Write-Host "== 1/7 云端旧版本快照（回滚点）"
if ($migrated) {
    Invoke-Remote "sudo -n mkdir -p /opt/backup; sudo -n tar czf /opt/backup/code-$Ts.tgz -C /opt/nonebot-releases $Prev; sudo -n ls -l /opt/backup/code-$Ts.tgz"
} else {
    Invoke-Remote "sudo -n mkdir -p /opt/backup; sudo -n tar czf /opt/backup/code-$Ts.tgz -C /opt/nonebot --ignore-failed-read app_core plugins adapters companion_core personas ops bot.py config.example.yaml requirements.txt README.md .deployed_sha; sudo -n ls -l /opt/backup/code-$Ts.tgz"
}

Write-Host "== 2/7 构建 release：$RelDir"
Invoke-Remote "sudo -n rm -rf $RelDir; sudo -n mkdir -p $RelDir/ops"
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
Invoke-Remote "sudo -n tar -xf /tmp/nonebot-$Ts.tar -C $RelDir; sudo -n tar -xf /tmp/ops-$Ts.tar -C $RelDir/ops; sudo -n chmod +x $RelDir/ops/*.sh; sudo -n chown -R ubuntu:ubuntu $RelDir; sudo -n bash $RelDir/ops/release-switch.sh prepare $RelDir"
Invoke-Remote "echo $Sha | sudo -n tee $RelDir/.deployed_sha > /dev/null"
Invoke-Remote "rm -f /tmp/nonebot-$Ts.tar /tmp/ops-$Ts.tar"

if (-not $SkipVerify) {
    Write-Host "== 3/7 release 内验证（--pre，不碰生产库、不发消息）"
    & ssh @SshOpts $SshHost "sudo -n $RelDir/ops/verify.sh --pre --code $RelDir"
    if ($LASTEXITCODE -ne 0) {
        Write-Host ""
        Write-Host "!! release 验证失败：生产 current / 版本标记都没动，服务仍跑旧版本。" -ForegroundColor Red
        Write-Host "!! 现场保留在 $RelDir（确认后 sudo rm -rf $RelDir）" -ForegroundColor Red
        Remove-Item -Recurse -Force $Tmp -ErrorAction SilentlyContinue
        exit 1
    }
} else {
    Write-Host "== 3/7 跳过 release 验证（-SkipVerify）"
}

if ($NoRestart) {
    Write-Host "== 完成：release 已验证，未切换、未重启（-NoRestart）；release：$RelDir"
    Remove-Item -Recurse -Force $Tmp -ErrorAction SilentlyContinue
    exit 0
}

Write-Host "== 4/7 原子切换 current → $RelId"
Invoke-Remote "sudo -n bash $RelDir/ops/release-switch.sh activate $RelId"
Invoke-Remote "echo deployed=`$(cat /opt/nonebot/.deployed_sha); systemctl is-active nonebot"

Write-Host "== 5/7 重启后全量验证（五件套）"
if (-not $NoPostVerify) {
    & ssh @SshOpts $SshHost "sudo -n /opt/nonebot/ops/verify.sh"
    if ($LASTEXITCODE -ne 0) {
        Write-Host "!! 全量验证有失败。服务已是新版本；回滚：" -ForegroundColor Red
        Write-Host "!! $Rollback" -ForegroundColor Red
        Remove-Item -Recurse -Force $Tmp -ErrorAction SilentlyContinue
        exit 1
    }
}

Write-Host "== 6/7 旧 release 保留在 /opt/nonebot-releases（手工按需清理）"
Write-Host "== 7/7 完成：HEAD $Sha 已部署；回滚目标 $PrevRel"
Write-Host "== 回滚：$Rollback"
Write-Host "== 快照：/opt/backup/code-$Ts.tgz"
Remove-Item -Recurse -Force $Tmp -ErrorAction SilentlyContinue
