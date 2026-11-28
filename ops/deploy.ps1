# Windows 发布入口。发布流程由 deploy.py 提供。
param(
    [string]$SshHost = $env:QQBOT_HOST,
    [string]$Key = $env:QQBOT_KEY,
    [switch]$NoRestart,
    [switch]$SkipVerify,
    [switch]$NoPostVerify
)

$DeployArgs = @()
if ($SshHost) { $DeployArgs += @("--host", $SshHost) }
if ($Key) { $DeployArgs += @("--key", $Key) }
if ($NoRestart) { $DeployArgs += "--no-restart" }
if ($SkipVerify) { $DeployArgs += "--skip-verify" }
if ($NoPostVerify) { $DeployArgs += "--no-post-verify" }
& python (Join-Path $PSScriptRoot "deploy.py") @DeployArgs
exit $LASTEXITCODE
