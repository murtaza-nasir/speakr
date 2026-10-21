param(
    [ValidateSet('Inspect','Start','StopVerified')][string]$Action,
    [Parameter(Mandatory)][string]$Root,
    [string]$BridgeTask = 'Speakr Recording Bridge',
    [string]$OllamaTask = 'Speakr Ollama',
    [ValidateSet('bridge','ollama')][string]$Component,
    [int]$ProcessId,
    [string]$StartMarker
)
$ErrorActionPreference = 'Stop'
$migration=Join-Path $Root 'adoption-state.json'
if ((Test-Path -LiteralPath $migration) -and (Get-Content -Raw -Encoding UTF8 -LiteralPath $migration | ConvertFrom-Json).phase -ne 'installed') { throw 'Partial adoption; runtime control refused' }
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$cfg = Get-Content -Raw -Encoding UTF8 -LiteralPath (Join-Path $Root 'config.json') | ConvertFrom-Json
function Read-Task([string]$Name) {
    $task = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
    if (!$task) { return @{enabled=$false;state='Absent';trusted=$false} }
    $expected = if ($Name -eq $BridgeTask) { Join-Path $Root 'bridge.py' } else { $cfg.ollama_launcher }
    $expectedExe = if ($Name -eq $BridgeTask) {$cfg.pythonw} else {(Get-Command powershell.exe).Source}
    $expectedArgs = if ($Name -eq $BridgeTask) {'"'+$expected+'" --loop'} else {'-NoProfile -WindowStyle Hidden -File "'+$expected+'" -Root "'+$Root+'"'}
    $trusted = @($task.Actions).Count -eq 1 -and $task.Actions[0].Execute -eq $expectedExe -and $task.Actions[0].Arguments -eq $expectedArgs -and $task.Actions[0].WorkingDirectory -eq $Root
    return @{enabled=($task.State -ne 'Disabled');state=[string]$task.State;trusted=$trusted}
}
function Verified-Process([int]$TargetId, [string]$Marker) {
    if (!$TargetId -or !$Marker) { return $false }
    $proc = Get-Process -Id $TargetId -ErrorAction SilentlyContinue
    if (!$proc -or [string]$proc.StartTime.ToFileTimeUtc() -ne $Marker) { return $false }
    $cim = Get-CimInstance Win32_Process -Filter "ProcessId=$TargetId"
    return $cim.CommandLine -and $cim.CommandLine.Contains((Join-Path $Root 'bridge.py'))
}
if ($Action -eq 'Inspect') {
    $b = Read-Task $BridgeTask
    $o = Read-Task $OllamaTask
    $heartbeatPath = Join-Path $Root 'heartbeat.json'
    $b.process_matches = $false
    if (Test-Path -LiteralPath $heartbeatPath) {
        $h = Get-Content -Encoding UTF8 -LiteralPath $heartbeatPath -Raw | ConvertFrom-Json
        $b.process_matches = Verified-Process $h.pid $h.start_marker
    }
    $cfg = Get-Content -Encoding UTF8 -LiteralPath (Join-Path $Root 'config.json') -Raw | ConvertFrom-Json
    # Bound the Docker CLI itself. A timeout of the parent PowerShell leaves an
    # unresponsive docker.exe orphaned at every watchdog tick.
    $absent = $null
    $probe = $null
    try {
        $start = [Diagnostics.ProcessStartInfo]::new()
        $start.FileName = $cfg.docker
        $start.Arguments = 'ps --format "{{.Names}}"'
        $start.UseShellExecute = $false
        $start.CreateNoWindow = $true
        $start.RedirectStandardOutput = $true
        $start.RedirectStandardError = $true
        $probe = [Diagnostics.Process]::Start($start)
        if (!$probe.WaitForExit(5000)) {
            $probe.Kill()
            if (!$probe.WaitForExit(2000)) { throw 'Docker probe did not terminate' }
        } elseif ($probe.ExitCode -eq 0) {
            $names = @($probe.StandardOutput.ReadToEnd() -split '\r?\n' | Where-Object { $_ })
            $absent = @($cfg.container_names | Where-Object { $_ -notin $names }).Count -gt 0
        }
    } catch {
        $absent = $null  # Unknown health never authorizes container mutation.
    } finally {
        if ($probe) { $probe.Dispose() }
    }
    @{bridge=$b;ollama_task=$o;containers_absent=$absent} | ConvertTo-Json -Depth 4 -Compress
    exit 0
}
if (Test-Path -LiteralPath (Join-Path $Root 'maintenance.json')) {
    $m = Get-Content -Encoding UTF8 -LiteralPath (Join-Path $Root 'maintenance.json') -Raw | ConvertFrom-Json
    if ($m.paused) { throw 'Maintenance is active' }
}
$name = if ($Component -eq 'bridge') {$BridgeTask} else {$OllamaTask}
$t = Read-Task $name
if (!$t.enabled -or !$t.trusted) { throw 'Task disabled, absent, or definition differs' }
if ($Action -eq 'Start') {
    if ($Component -eq 'ollama') {
        $listeners=@(Get-NetTCPConnection -State Listen -ErrorAction Stop | Where-Object LocalPort -eq $cfg.llm_port)
        if ($listeners) {
            foreach ($listener in $listeners) {
                $owner=Get-Process -Id $listener.OwningProcess -ErrorAction Stop
                if ($owner.Path -ne $cfg.ollama -or $listener.LocalAddress -ne '127.0.0.1') { throw 'Ollama endpoint owned by a different process' }
            }
            '{"status":"existing_owned_server"}'
            exit 0
        }
    }
    if ($t.state -eq 'Ready') { Start-ScheduledTask -TaskName $name }
} elseif ($Action -eq 'StopVerified') {
    if ($Component -ne 'bridge' -or !(Verified-Process $ProcessId $StartMarker)) { throw 'Process identity differs' }
    $latest = Get-Content -Encoding UTF8 -LiteralPath (Join-Path $Root 'heartbeat.json') -Raw | ConvertFrom-Json
    $now = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()/1000.0
    if ($latest.pid -ne $ProcessId -or $latest.start_marker -ne $StartMarker -or
        ($now - $latest.updated) -lt 45 -or !$latest.deadline -or $now -lt ($latest.deadline + 60)) { throw 'Worker revived or deadline differs; not stopped' }
    $maintenance = Join-Path $Root 'maintenance.json'
    if ((Test-Path -LiteralPath $maintenance) -and (Get-Content -Encoding UTF8 -LiteralPath $maintenance -Raw | ConvertFrom-Json).paused) { throw 'Maintenance became active' }
    Stop-Process -Id $ProcessId -ErrorAction Stop
}
'{}'
