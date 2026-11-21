# deploy.ps1 —— Windows 无 bash 时的等价部署入口，门禁顺序与 ops/deploy.sh 相同。
#
# 为什么需要：本地没有 bash（where bash 为空），而 deploy.sh 是唯一的部署入口。
# 这个脚本只做「等价搬运」，不改变门禁顺序：
#   快照（完整发布范围）→ 解到候选目录 → 候选目录 verify --pre 过了才切换 → 重启 → 全量 verify。
# 旧流程先覆盖生产再验证，--pre 失败时磁盘已是新旧混合体；现在验证失败时生产目录不动。
#
# 用法：
#   $env:QQBOT_HOST = "ubuntu@150.158.90.134"
#   $env:QQBOT_KEY  = "C:\Users\59911\Documents\Projects\qq-bot-v2\qq_bot.pem"
#   powershell -File ops/deploy.ps1
#
#   powershell -File ops/deploy.ps1 -NoRestart      # 只建候选并验证，不切换
#   powershell -File ops/deploy.ps1 -SkipVerify     # 跳过候选验证（紧急回滚用）
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
$Cand = "/opt/nonebot-candidate-$Ts"
$Tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("qqbot-deploy-" + $Ts)
New-Item -ItemType Directory -Force -Path $Tmp | Out-Null

$SshOpts = @("-i", $Key, "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=20")

function Invoke-Remote([string]$Cmd) {
    & ssh @SshOpts $SshHost $Cmd
    if ($LASTEXITCODE -ne 0) { throw "远端命令失败（exit $LASTEXITCODE）：$Cmd" }
}

$dirty = (git status --porcelain)
if ($dirty) { Write-Host "WARN 工作区有未提交改动，部署的是 HEAD（$Sha），不是当前工作区文件" -ForegroundColor Yellow }
$Rollback = "ssh $SshHost 'sudo -n tar xzf /opt/backup/code-$Ts.tgz -C /opt/nonebot && sudo -n find /opt/nonebot -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null; sudo -n systemctl restart nonebot'"

Write-Host "== 1/7 云端旧代码快照（完整发布范围，回滚点）"
# 覆盖范围 = 本脚本会上传的全部内容；不含 venv/data，它们不参与发布。
Invoke-Remote "sudo -n mkdir -p /opt/backup; sudo -n tar czf /opt/backup/code-$Ts.tgz -C /opt/nonebot --ignore-failed-read app_core plugins adapters companion_core personas ops bot.py config.example.yaml requirements.txt README.md .deployed_sha; sudo -n ls -l /opt/backup/code-$Ts.tgz"

Write-Host "== 2/7 上传 HEAD:$Sha 到候选目录 $Cand"
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
Invoke-Remote "sudo -n rm -rf $Cand; sudo -n mkdir -p $Cand/ops; sudo -n tar -xf /tmp/nonebot-$Ts.tar -C $Cand; sudo -n tar -xf /tmp/ops-$Ts.tar -C $Cand/ops; sudo -n ln -sfn /opt/nonebot/venv $Cand/venv; sudo -n chmod +x $Cand/ops/*.sh; sudo -n chown -R ubuntu:ubuntu $Cand"

# 删除清单：tar 解压是覆盖写，不会删掉已从 HEAD 移除的文件。
# 路径映射必须去掉 nonebot/ 前缀：/opt/nonebot 下没有第二层 nonebot。
$prevSha = @(& ssh @SshOpts $SshHost "cat /opt/nonebot/.deployed_sha 2>/dev/null")[0]
if ($prevSha) { $prevSha = $prevSha.Trim() }
if ($prevSha -and (git rev-parse --verify --quiet ($prevSha + "^{commit}"))) {
    $deleted = @(git diff --name-only --diff-filter=D $prevSha $Sha -- nonebot ops)
    foreach ($f in $deleted) {
        if (-not $f) { continue }
        if ($f -like "nonebot/*") {
            $rel = "/opt/nonebot/" + $f.Substring(8)
        } elseif ($f -like "ops/*") {
            $rel = "/opt/nonebot/ops/" + $f.Substring(4)
        } else { continue }
        Invoke-Remote "sudo -n rm -f '$rel'"
    }
    if ($deleted.Count -gt 0) { Write-Host ("== 2.5/7 删除已移除的文件：" + ($deleted -join ", ")) }
} else {
    Write-Host "WARN 读不到上次部署 sha，跳过删除清单（云端可能残留已删文件）" -ForegroundColor Yellow
}

if (-not $SkipVerify) {
    Write-Host "== 3/7 候选目录验证（--pre，不碰生产库、不发消息，生产目录未被覆盖）"
    & ssh @SshOpts $SshHost "sudo -n $Cand/ops/verify.sh --pre --code $Cand"
    if ($LASTEXITCODE -ne 0) {
        Write-Host ""
        Write-Host "!! 候选目录 verify --pre 失败：生产目录与版本标记都没动，服务仍在跑旧代码。" -ForegroundColor Red
        Write-Host "!! 排查现场保留在 $Cand（确认后 sudo rm -rf $Cand）" -ForegroundColor Red
        Remove-Item -Recurse -Force $Tmp -ErrorAction SilentlyContinue
        exit 1
    }
} else {
    Write-Host "== 3/7 跳过候选验证（-SkipVerify）"
}

if ($NoRestart) {
    Write-Host "== 完成：候选目录已验证，未切换、未重启（-NoRestart）；候选：$Cand"
    Remove-Item -Recurse -Force $Tmp -ErrorAction SilentlyContinue
    exit 0
}

Write-Host "== 4/7 切换：覆盖发布范围，版本标记随切换更新"
Invoke-Remote "cd $Cand && sudo -n tar --exclude=venv --exclude=__pycache__ --exclude=.deployed_sha -cf - . | sudo -n tar -C /opt/nonebot -xf -"
Invoke-Remote "sudo -n chown -R ubuntu:ubuntu /opt/nonebot/app_core /opt/nonebot/adapters /opt/nonebot/plugins /opt/nonebot/ops /opt/nonebot/companion_core /opt/nonebot/personas 2>/dev/null || true"
Invoke-Remote "sudo -n find /opt/nonebot -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true"
Invoke-Remote "sudo -n chmod +x /opt/nonebot/ops/*.sh"
Invoke-Remote "echo $Sha | sudo -n tee /opt/nonebot/.deployed_sha > /dev/null"
Invoke-Remote "cat /opt/nonebot/.deployed_sha"
Invoke-Remote "rm -f /tmp/nonebot-$Ts.tar /tmp/ops-$Ts.tar"

Write-Host "== 5/7 重启并确认存活"
Invoke-Remote "sudo -n systemctl restart nonebot; sleep 5; systemctl is-active nonebot"
Invoke-Remote "cat /opt/nonebot/.deployed_sha; systemctl is-active nonebot"

if (-not $NoPostVerify) {
    Write-Host "== 6/7 重启后全量验证（五件套）"
    & ssh @SshOpts $SshHost "sudo -n /opt/nonebot/ops/verify.sh"
    if ($LASTEXITCODE -ne 0) {
        Write-Host "!! 全量验证有失败。服务已是新代码；先看失败项，必要时回滚：" -ForegroundColor Red
        Write-Host "!! $Rollback" -ForegroundColor Red
        Remove-Item -Recurse -Force $Tmp -ErrorAction SilentlyContinue
        exit 1
    }
}

Write-Host "== 7/7 清理候选目录"
Invoke-Remote "sudo -n rm -rf $Cand"
Write-Host "== 完成：HEAD $Sha 已部署；回滚点 /opt/backup/code-$Ts.tgz"
Write-Host "== 回滚：$Rollback"
Remove-Item -Recurse -Force $Tmp -ErrorAction SilentlyContinue
