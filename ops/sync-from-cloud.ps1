# sync-from-cloud.ps1 —— 把云端的每日备份拉到本地。
#
# 为什么是「拉」不是「推」：家里没有公网 IP，云端推不过来；本地也不保证 24 小时开机。
# 所以改成「开机/手动时补拉」：云端留 7 天，本地留 30 天。
#
# 配置（不入库）：备份/sync-config.json
#   { "host": "root@server", "key": "C:/path/key.pem",
#     "local_dir": "C:/.../备份/云端", "keep_days": 30 }
# 模板见 ops/sync-config.example.json
#
# 用法：powershell -File ops/sync-from-cloud.ps1
# 自启：Startup 里的 companion-backup.vbs（隐藏窗口）

$ErrorActionPreference = "Continue"

$Root       = Split-Path $PSScriptRoot -Parent
$ConfigFile = Join-Path $Root "备份\sync-config.json"
$LogFile    = Join-Path $Root "备份\sync.log"
$MaxPull    = 5

function Write-Log($msg) {
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg
    Add-Content -Path $LogFile -Value $line -Encoding UTF8
}

New-Item -ItemType Directory -Force -Path (Split-Path $LogFile) | Out-Null
Write-Log "=== pull start ==="

if (-not (Test-Path $ConfigFile)) {
    Write-Log "缺少配置：$ConfigFile（模板见 ops/sync-config.example.json）"
    exit 2
}
$cfg = Get-Content $ConfigFile -Raw | ConvertFrom-Json
$Key      = [string]$cfg.key
$SshHost  = [string]$cfg.host
$LocalDir = [string]$cfg.local_dir
$KeepDays = if ($cfg.keep_days) { [int]$cfg.keep_days } else { 30 }
if (-not $Key -or -not $SshHost -or -not $LocalDir) {
    Write-Log "配置缺字段（host / key / local_dir）"
    exit 2
}

New-Item -ItemType Directory -Force -Path $LocalDir | Out-Null

$remote = & ssh -i $Key -o StrictHostKeyChecking=no -o ConnectTimeout=10 $SshHost `
    "ls -1t /opt/backup/cloud-*.tgz 2>/dev/null | head -$MaxPull"
if ($LASTEXITCODE -ne 0) {
    Write-Log "ssh 失败（exit $LASTEXITCODE），跳过"
    exit 1
}

$pulled = 0
foreach ($f in @($remote)) {
    $f = "$f".Trim()
    if (-not $f) { continue }
    $name = Split-Path $f -Leaf
    $dst = Join-Path $LocalDir $name
    if (Test-Path $dst) { continue }

    & scp -i $Key -o StrictHostKeyChecking=no -o ConnectTimeout=10 "${SshHost}:${f}" $dst 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Log "scp 失败：$name"
        continue
    }
    & tar -tzf $dst > $null 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Log "校验失败，删除半个文件：$name"
        Remove-Item $dst -Force -ErrorAction SilentlyContinue
        continue
    }
    $size = [math]::Round((Get-Item $dst).Length / 1MB, 1)
    Write-Log "已拉取 $name（${size}MB）"
    $pulled++
}

Get-ChildItem $LocalDir -Filter "cloud-*.tgz" |
    Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-$KeepDays) } |
    ForEach-Object { Write-Log "清理过期 $($_.Name)"; Remove-Item $_.FullName -Force }

$total = (Get-ChildItem $LocalDir -Filter "cloud-*.tgz" | Measure-Object).Count
Write-Log "=== pull done：新增 $pulled，本地共 $total 份 ==="
