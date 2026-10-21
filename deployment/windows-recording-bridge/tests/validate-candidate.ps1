param([string]$Root = (Join-Path $env:TEMP ('speakr-recovery-' + [guid]::NewGuid().ToString('N'))))
$ErrorActionPreference = 'Stop'
$package = Split-Path $PSScriptRoot -Parent
$Root = [IO.Path]::GetFullPath($Root)
if ($Root -eq 'C:\Programs\Speakr\automation' -or (Test-Path -LiteralPath $Root)) { throw 'Fresh isolated root required' }
$suffix = [guid]::NewGuid().ToString('N').Substring(0,8)
$bridgeTask = 'Speakr Candidate Bridge ' + $suffix
$ollamaTask = 'Speakr Candidate Ollama ' + $suffix
$watchdogTask = 'Speakr Candidate Watchdog ' + $suffix
$python = 'C:\Python314\python.exe'
New-Item -ItemType Directory -Path $Root -Force | Out-Null
$originalRoot = 'C:\Programs\Speakr\automation'
$baseline = Get-Content -Encoding UTF8 -LiteralPath (Join-Path $package 'baseline-manifest.json') -Raw | ConvertFrom-Json
foreach ($item in $baseline.PSObject.Properties) {
    Copy-Item -LiteralPath (Join-Path $originalRoot $item.Name) -Destination $Root
}
New-Item -ItemType Directory -Path (Join-Path $Root 'vault') -Force | Out-Null
@{sources=@();vault_root=(Join-Path $Root 'vault');vault=(Join-Path $Root 'vault\Расшифровки');stable_seconds=1;scan_seconds=1;
  completed_poll_seconds=0;speakr_url='http://127.0.0.1:1';auto_start_docker=$false;ffprobe='ffprobe';ffmpeg='ffmpeg';
  docker='C:\Program Files\Docker\Docker\resources\bin\docker.exe';docker_desktop='C:\Program Files\Docker\Docker\Docker Desktop.exe';
  compose_file=(Join-Path $Root 'not-used.yaml');pythonw='C:\Python314\pythonw.exe'} | ConvertTo-Json -Depth 3 | Set-Content -LiteralPath (Join-Path $Root 'config.json') -Encoding UTF8
$action = New-ScheduledTaskAction -Execute 'C:\Python314\pythonw.exe' -Argument ('"' + (Join-Path $Root 'bridge.py') + '" --loop') -WorkingDirectory $Root
$principal = New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
$backup = $null
try {
    Register-ScheduledTask -TaskName $bridgeTask -Action $action -Principal $principal -Settings $settings | Out-Null
    Disable-ScheduledTask -TaskName $bridgeTask | Out-Null
    $originalConfigHash = (Get-FileHash -LiteralPath (Join-Path $Root 'config.json')).Hash
    $originalTaskXml = Export-ScheduledTask -TaskName $bridgeTask
    foreach ($failurePoint in @('Manifest','Backup','BeforeReplace','MiddleCopy','AfterCopy','AfterTask')) {
        $failed = $false
        try {
            & (Join-Path $package 'install-recovery.ps1') -TargetRoot $Root -Candidate -Apply -BridgeTask $bridgeTask -OllamaTask $ollamaTask -WatchdogTask $watchdogTask -InjectFailure $failurePoint | Out-Null
        } catch { $failed = $_.Exception.Message -match 'Injected'; if (!$failed) { Write-Host $_.Exception.Message } }
        if (!$failed) { throw ('Failure did not cleanly roll back: ' + $failurePoint) }
        if ((Get-FileHash -LiteralPath (Join-Path $Root 'config.json')).Hash -ne $originalConfigHash) { throw ('Failure changed config: ' + $failurePoint) }
        if ((Export-ScheduledTask -TaskName $bridgeTask) -ne $originalTaskXml) { throw ('Failure changed task: ' + $failurePoint) }
        if ((Test-Path -LiteralPath (Join-Path $Root 'maintenance.json')) -or (Get-ScheduledTask -TaskName $watchdogTask -ErrorAction SilentlyContinue)) { throw ('Failure left maintenance/watchdog: ' + $failurePoint) }
        foreach ($item in $baseline.PSObject.Properties) {
            if ((Get-FileHash -LiteralPath (Join-Path $Root $item.Name)).Hash -ne $item.Value) { throw ('Failure changed original: ' + $failurePoint) }
        }
        if (Test-Path -LiteralPath (Join-Path $Root 'recovery.py')) { throw ('Failure left candidate code: ' + $failurePoint) }
    }
    $installedRaw = & (Join-Path $package 'install-recovery.ps1') -TargetRoot $Root -Candidate -Apply -BridgeTask $bridgeTask -OllamaTask $ollamaTask -WatchdogTask $watchdogTask
    $installed = ($installedRaw -join "`n") | ConvertFrom-Json
    $backup = $installed.backup
    if ($installed.status -ne 'installed_paused') { throw 'Unexpected install result' }
    Start-ScheduledTask -TaskName $watchdogTask
    $deadline = [DateTime]::UtcNow.AddSeconds(30)
    do { Start-Sleep -Milliseconds 250 } while (!(Test-Path -LiteralPath (Join-Path $Root 'watchdog-status.json')) -and [DateTime]::UtcNow -lt $deadline)
    $watchState = Get-Content -Encoding UTF8 -LiteralPath (Join-Path $Root 'watchdog-status.json') -Raw | ConvertFrom-Json
    if (!$watchState.maintenance) { throw 'Watchdog did not honour maintenance' }
    # A running bridge must publish heartbeat even when maintenance is active.
    Enable-ScheduledTask -TaskName $bridgeTask | Out-Null
    Start-ScheduledTask -TaskName $bridgeTask
    $deadline = [DateTime]::UtcNow.AddSeconds(30)
    do { Start-Sleep -Milliseconds 250 } while (!(Test-Path -LiteralPath (Join-Path $Root 'heartbeat.json')) -and [DateTime]::UtcNow -lt $deadline)
    $h = Get-Content -Encoding UTF8 -LiteralPath (Join-Path $Root 'heartbeat.json') -Raw | ConvertFrom-Json
    $process = Get-Process -Id $h.pid
    if ([string]$process.StartTime.ToFileTimeUtc() -ne [string]$h.start_marker) { throw 'Heartbeat process identity differs' }
    # Fault injection affects only our empty candidate worker. Health URLs are
    # deliberately unreachable; candidate runtime cannot start Docker/containers.
    Stop-ScheduledTask -TaskName $bridgeTask
    @{paused=$false} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $Root 'maintenance.json') -Encoding UTF8
    & $python (Join-Path $Root 'watchdog.py') --config (Join-Path $Root 'config.json')
    if ($LASTEXITCODE -ne 0) { throw 'Native watchdog pass failed' }
    $oldPid = $h.pid
    $deadline = [DateTime]::UtcNow.AddSeconds(30)
    do {
        Start-Sleep -Milliseconds 250
        $h = Get-Content -Encoding UTF8 -LiteralPath (Join-Path $Root 'heartbeat.json') -Raw | ConvertFrom-Json
    } while ($h.pid -eq $oldPid -and [DateTime]::UtcNow -lt $deadline)
    if ($h.pid -eq $oldPid) { throw 'Stopped worker was not recovered' }
    $process = Get-Process -Id $h.pid
    if ([string]$process.StartTime.ToFileTimeUtc() -ne [string]$h.start_marker) { throw 'Recovered worker identity differs' }
    $refused = $false
    try {
        & (Join-Path $Root 'runtime-control.ps1') -Action StopVerified -Root $Root -BridgeTask $bridgeTask -OllamaTask $ollamaTask -Component bridge -ProcessId $h.pid -StartMarker $h.start_marker | Out-Null
    } catch { $refused = $true }
    if (!$refused -or !(Get-Process -Id $h.pid -ErrorAction SilentlyContinue)) { throw 'Fresh heartbeat did not protect healthy worker' }
    @{paused=$true} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $Root 'maintenance.json') -Encoding UTF8
    Disable-ScheduledTask -TaskName $bridgeTask | Out-Null
    $xml = Export-ScheduledTask -TaskName $watchdogTask
    $xml | Set-Content -LiteralPath (Join-Path $Root 'watchdog-task.xml') -Encoding UTF8
    if ($xml -notmatch '<Interval>PT1M</Interval>' -or $xml -notmatch 'LogonTrigger') { throw 'Watchdog triggers incomplete' }
    $configPath = Join-Path $Root 'config.json'
    $configBytes = [IO.File]::ReadAllBytes($configPath)
    $bridgeHash = (Get-FileHash -LiteralPath (Join-Path $Root 'bridge.py')).Hash
    Add-Content -LiteralPath $configPath -Value ' ' -Encoding UTF8
    $refused = $false
    try { & (Join-Path $package 'install-recovery.ps1') -TargetRoot $Root -Candidate -Rollback -BackupPath $backup -BridgeTask $bridgeTask -OllamaTask $ollamaTask -WatchdogTask $watchdogTask | Out-Null }
    catch { $refused = $_.Exception.Message -match 'config conflict' }
    if (!$refused -or (Get-FileHash -LiteralPath (Join-Path $Root 'bridge.py')).Hash -ne $bridgeHash) { throw 'Config drift caused partial rollback' }
    [IO.File]::WriteAllBytes($configPath, $configBytes)
    Enable-ScheduledTask -TaskName $bridgeTask | Out-Null
    $refused = $false
    try { & (Join-Path $package 'install-recovery.ps1') -TargetRoot $Root -Candidate -Rollback -BackupPath $backup -BridgeTask $bridgeTask -OllamaTask $ollamaTask -WatchdogTask $watchdogTask | Out-Null }
    catch { $refused = $_.Exception.Message -match 'task conflict' }
    if (!$refused -or (Get-FileHash -LiteralPath (Join-Path $Root 'bridge.py')).Hash -ne $bridgeHash) { throw 'Task drift caused partial rollback' }
    Disable-ScheduledTask -TaskName $bridgeTask | Out-Null
    $rollbackRaw = & (Join-Path $package 'install-recovery.ps1') -TargetRoot $Root -Candidate -Rollback -BackupPath $backup -BridgeTask $bridgeTask -OllamaTask $ollamaTask -WatchdogTask $watchdogTask
    $restored = Get-ScheduledTask -TaskName $bridgeTask
    if ($restored.State -ne 'Disabled') { throw 'Rollback resurrected originally disabled bridge' }
    if (Get-ScheduledTask -TaskName $watchdogTask -ErrorAction SilentlyContinue) { throw 'New watchdog not removed by rollback' }
    foreach ($item in $baseline.PSObject.Properties) {
        if ((Get-FileHash -LiteralPath (Join-Path $Root $item.Name)).Hash -ne $item.Value) { throw ('Rollback hash differs: ' + $item.Name) }
    }
    @{status='PASS';root=$Root;partialInstallRollback=$true;installedPaused=$true;watchdogHonouredMaintenance=$true;
      heartbeatIdentity=$true;stoppedWorkerRecovered=$true;healthyRevivalProtected=$true;rollbackConfigDriftProtected=$true;rollbackTaskDriftProtected=$true;
      logonAndMinuteTriggers=$true;rollbackPreservedDisabled=$true;liveChanged=$false} |
      ConvertTo-Json | Tee-Object -FilePath (Join-Path $Root 'candidate-validation.json')
} finally {
    foreach ($name in @($bridgeTask,$watchdogTask)) {
        $task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
        if ($task) { Stop-ScheduledTask -TaskName $name; Unregister-ScheduledTask -TaskName $name -Confirm:$false }
    }
}
