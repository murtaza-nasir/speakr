param([ValidateSet('Pause','Resume','Status','ApproveBacklog')][string]$Action = 'Status', [string]$Root = $PSScriptRoot, [string[]]$JobIds)
$ErrorActionPreference = 'Stop'
$cfg = Get-Content -Encoding UTF8 -LiteralPath (Join-Path $Root 'config.json') -Raw | ConvertFrom-Json
if ($Action -eq 'ApproveBacklog') {
    if (!$JobIds) { throw 'Explicit JobIds required; preview and separate backlog approval first' }
    foreach ($id in $JobIds) { if ($id -notmatch '^[0-9a-f]{32}$' -or $id -notin $cfg.legacy_recovery_jobs) { throw 'Unknown legacy job id' } }
    $path = Join-Path $Root 'backlog-approved.json'
    $tmp = $path + '.' + [guid]::NewGuid().ToString('N')
    @{job_ids=$JobIds} | ConvertTo-Json | Set-Content -LiteralPath $tmp -Encoding UTF8
    Move-Item -LiteralPath $tmp -Destination $path -Force
    exit 0
}
if ($Action -eq 'Status') {
    foreach ($file in @('status.json','watchdog-status.json','maintenance.json')) {
        $path = Join-Path $Root $file
        if (Test-Path -LiteralPath $path) { Get-Content -Encoding UTF8 -LiteralPath $path -Raw }
    }
    exit 0
}
$path = Join-Path $Root 'maintenance.json'
$tmp = $path + '.' + [guid]::NewGuid().ToString('N')
@{paused=($Action -eq 'Pause');updated=[DateTimeOffset]::UtcNow.ToUnixTimeSeconds()} | ConvertTo-Json | Set-Content -LiteralPath $tmp -Encoding UTF8
Move-Item -LiteralPath $tmp -Destination $path -Force
# Neither operation changes discovery pause or task Enabled flags.
if ($Action -eq 'Resume') {
    foreach ($name in @($cfg.bridge_task,$cfg.watchdog_task)) {
        $task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
        if ($task -and $task.State -eq 'Ready') { Start-ScheduledTask -TaskName $name }
    }
}
