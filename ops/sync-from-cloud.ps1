# sync-from-cloud.ps1 —— 把云端的每日备份拉到本地。
#
# 为什么是「拉」不是「推」：家里没有公网 IP，云端推不过来；本地也不保证 24 小时开机。
# 所以改成「开机/手动时补拉」：云端留 7 天，本地留 30 天。
#
# 配置（不入库）：备份/sync-config.json
#   { "host": "root@server", "key": "C:/path/key.pem",
#     "local_dir": "C:/Users/you/Backups/companion-bot", "keep_days": 30 }
# 模板见 ops/sync-config.example.json
# local_dir 必须在共享工作区（Documents/ai）和本仓库之外。备份包含 keys.json。
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
# 必须显式 -Encoding UTF8：PS 5.1 的 Get-Content 默认按 ANSI/GBK 解码，
# 无 BOM 的 UTF-8 配置会把「备份/云端」读成「澶囦唤/浜戠」——备份就被写进
# 一个乱码目录里，而正常目录看起来「停在 11-11」（2026-11-15 实测：
# 11-12~11-14 三份都写错了位置，恢复时差点漏掉最近三天）。
$cfg = Get-Content $ConfigFile -Raw -Encoding UTF8 | ConvertFrom-Json
$Key      = [string]$cfg.key
$SshHost  = [string]$cfg.host
$LocalDir = [string]$cfg.local_dir
$KeepDays = if ($cfg.keep_days) { [int]$cfg.keep_days } else { 30 }
if (-not $Key -or -not $SshHost -or -not $LocalDir) {
    Write-Log "配置缺字段（host / key / local_dir）"
    exit 2
}

function Test-BackupDirAllowed([string]$Path) {
    # 备份包含 keys.json。拉进共享工作区或仓库会把凭证副本放进共享目录。
    $full = [System.IO.Path]::GetFullPath($Path)
    $shared = [System.IO.Path]::GetFullPath((Join-Path $env:USERPROFILE "Documents\ai"))
    $bans = @($Root, $shared)
    foreach ($ban in $bans) {
        if (-not $ban) { continue }
        $base = $ban.TrimEnd('\')
        $prefix = $base + '\'
        if ($full.Equals($base, [StringComparison]::OrdinalIgnoreCase) -or
            $full.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
            return $false
        }
    }
    return $true
}

if (-not (Test-BackupDirAllowed $LocalDir)) {
    Write-Log "拒绝拉取：local_dir 在共享工作区或仓库内（$LocalDir）。备份含 keys.json，必须放到工作区外的受限目录。"
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
    if (Test-Path $dst) {
        & tar -tzf $dst > $null 2>&1
        if ($LASTEXITCODE -eq 0) { continue }
        # 上次 scp 中断留下的半包不能因为同名就永远跳过。
        Write-Log "已有文件校验失败，重新拉取：$name"
        Remove-Item $dst -Force -ErrorAction SilentlyContinue
    }

    $part = Join-Path $LocalDir (".partial-" + $name)
    if (Test-Path $part) { Remove-Item $part -Force -ErrorAction SilentlyContinue }
    & scp -i $Key -o StrictHostKeyChecking=no -o ConnectTimeout=10 "${SshHost}:${f}" $part 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Log "scp 失败：$name"
        Remove-Item $part -Force -ErrorAction SilentlyContinue
        continue
    }
    & tar -tzf $part > $null 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Log "校验失败，删除半个文件：$name"
        Remove-Item $part -Force -ErrorAction SilentlyContinue
        continue
    }
    Move-Item -Force $part $dst
    $size = [math]::Round((Get-Item $dst).Length / 1MB, 1)
    Write-Log "已拉取 $name（${size}MB）"
    $pulled++
}

Get-ChildItem $LocalDir -Filter "cloud-*.tgz" |
    Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-$KeepDays) } |
    ForEach-Object { Write-Log "清理过期 $($_.Name)"; Remove-Item $_.FullName -Force }

# 在云端写「本地最近一次拉取成功」的时间戳：云端 healthcheck 用它判断异地副本
# 是否连续多天没人拉（本地不是常开，不能只看 offsite.state）。
# 最新一份没完整落地就不要更新：否则半包会被当成异地副本正常。
$newest = $null
foreach ($f in @($remote)) {
    $f = "$f".Trim()
    if ($f) { $newest = $f; break }
}
$stampOk = $false
if ($newest) {
    $newestLocal = Join-Path $LocalDir (Split-Path $newest -Leaf)
    if (Test-Path $newestLocal) {
        & tar -tzf $newestLocal > $null 2>&1
        if ($LASTEXITCODE -eq 0) { $stampOk = $true }
    }
}
if ($stampOk) {
    $epoch = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    & ssh -i $Key -o StrictHostKeyChecking=no -o ConnectTimeout=10 $SshHost `
        "echo $epoch > /opt/nonebot/ops/.local-pull-ts" 2>$null
    if ($LASTEXITCODE -ne 0) { Write-Log "写本地拉取时间戳失败（不影响本次备份）" }
} else {
    Write-Log "最新备份未完整落地，不更新拉取时间戳"
}

$total = (Get-ChildItem $LocalDir -Filter "cloud-*.tgz" | Measure-Object).Count
Write-Log "=== pull done：新增 $pulled，本地共 $total 份 ==="
