param(
    [string]$TargetRoot = 'C:\Programs\Speakr\automation',
    [switch]$DryRun,
    [switch]$Apply,
    [switch]$Rollback,
    [string]$BackupPath,
    [string]$BridgeTask = 'Speakr Recording Bridge',
    [string]$OllamaTask = 'Speakr Ollama',
    [string]$WatchdogTask = 'Speakr Recovery Watchdog',
    [string]$Python = 'C:\Python314\python.exe',
    [switch]$Candidate,
    [ValidateSet('None','Manifest','Backup','BeforeReplace','MiddleCopy','AfterCopy','AfterTask')][string]$InjectFailure = 'None'
)
$ErrorActionPreference = 'Stop'
$sourceRoot = $PSScriptRoot
$target = [IO.Path]::GetFullPath($TargetRoot).TrimEnd('\')
if ($target -eq [IO.Path]::GetPathRoot($target).TrimEnd('\')) { throw 'Target root cannot be a drive root' }
if (!$DryRun -and !$Apply -and !$Rollback) { $DryRun = $true }
if (!$Candidate -and $InjectFailure -ne 'None') { throw 'Failure injection is candidate-only' }
if ($Apply -and $Rollback) { throw 'Choose apply or rollback' }
if (!(Test-Path -LiteralPath (Join-Path $target 'config.json'))) { throw 'Existing config.json required; installer does not guess credentials/config' }
if ($Candidate -and $target -eq 'C:\Programs\Speakr\automation') { throw 'Candidate cannot target live automation' }
if ($Candidate -and ($BridgeTask -eq 'Speakr Recording Bridge' -or $OllamaTask -eq 'Speakr Ollama' -or $WatchdogTask -eq 'Speakr Recovery Watchdog')) { throw 'Candidate requires distinct task names' }
$cfg = Get-Content -Encoding UTF8 -LiteralPath (Join-Path $target 'config.json') -Raw | ConvertFrom-Json
$files = @('bridge.py','common.py','speakr_api.py','publication.py','provenance.py','backfill.py','enqueue.pyw',
           'recovery.py','health.py','watchdog.py','runtime-control.ps1','notify.ps1','recovery-control.ps1')
function Task-State([string]$Name) {
    $task = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
    if (!$task) { return @{exists=$false;name=$Name} }
    @{exists=$true;name=$Name;enabled=($task.State -ne 'Disabled');running=($task.State -eq 'Running');xml=(Export-ScheduledTask -TaskName $Name)}
}
function Task-Equivalent([string]$Left, [string]$Right) {
    if (!$Left -or !$Right) { return $false }
    function Canonical([string]$Text) {
        $service = New-Object -ComObject 'Schedule.Service'
        $service.Connect()
        $taskDef = $service.NewTask(0)
        $taskDef.XmlText = $Text
        [xml]$doc = $taskDef.XmlText
        $principal = $doc.Task.Principals.Principal
        $context = $doc.Task.Actions.GetAttribute('Context')
        if ($context -and $context -ne $principal.GetAttribute('id')) { throw 'Unexpected task principal context' }
        $principal.RemoveAttribute('id')
        $doc.Task.Actions.RemoveAttribute('Context')
        function Flatten($Node, [string]$Path) {
            foreach ($attr in $Node.Attributes) {
                if ($attr.Name -ne 'xmlns') { $Path + '/@' + $attr.Name + '=' + $attr.Value }
            }
            $children = @($Node.ChildNodes | Where-Object { $_.NodeType -eq 'Element' })
            if (!$children.Count) {
                $value = $Node.InnerText
                if ($Node.LocalName -eq 'UserId' -and $value) {
                    if ($value -notmatch '^S-1-') { $value = (New-Object Security.Principal.NTAccount($value)).Translate([Security.Principal.SecurityIdentifier]).Value }
                }
                $Path + '=' + $value
            } else {
                $indices = @{}
                foreach ($child in $children) {
                    $key = $child.LocalName
                    $indices[$key] = 1 + $indices[$key]
                    Flatten $child ($Path + '/' + $key + '[' + $indices[$key] + ']')
                }
            }
        }
        (@(Flatten $doc.DocumentElement '/Task' | Sort-Object) -join "`n")
    }
    (Canonical $Left) -eq (Canonical $Right)
}
function Put-Json($Path, $Value) {
    $tmp = $Path + '.new-' + [guid]::NewGuid().ToString('N')
    $Value | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $tmp -Encoding UTF8
    Move-Item -LiteralPath $tmp -Destination $Path -Force
}
function Copy-Atomic([string]$Source, [string]$Destination, [switch]$FailBeforeReplace) {
    $tmp = $Destination + '.install-' + [guid]::NewGuid().ToString('N')
    try {
        Copy-Item -LiteralPath $Source -Destination $tmp
        if ((Get-FileHash -LiteralPath $tmp).Hash -ne (Get-FileHash -LiteralPath $Source).Hash) { throw 'Staged copy hash mismatch' }
        if ($FailBeforeReplace) { throw 'Injected staged copy failure before replacement' }
        if (Test-Path -LiteralPath $Destination) { [IO.File]::Replace($tmp, $Destination, ($tmp + '.replaced')) }
        else { [IO.File]::Move($tmp, $Destination) }
    } finally {
        if (Test-Path -LiteralPath $tmp) { Remove-Item -LiteralPath $tmp }
        if (Test-Path -LiteralPath ($tmp + '.replaced')) { Remove-Item -LiteralPath ($tmp + '.replaced') }
    }
}
function Stop-Worker {
    Put-Json (Join-Path $target 'stop.json') @{at=([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()/1000.0)}
    $limit = [DateTime]::UtcNow.AddSeconds(45)
    do {
        $task = Get-ScheduledTask -TaskName $BridgeTask -ErrorAction SilentlyContinue
        $hPath = Join-Path $target 'heartbeat.json'
        $fresh = $false
        if (Test-Path -LiteralPath $hPath) {
            $h = Get-Content -Encoding UTF8 -LiteralPath $hPath -Raw | ConvertFrom-Json
            $proc = Get-Process -Id $h.pid -ErrorAction SilentlyContinue
            $fresh = $proc -and ([string]$proc.StartTime.ToFileTimeUtc() -eq [string]$h.start_marker)
        }
        if ((!$task -or $task.State -ne 'Running') -and !$fresh) { return }
        Start-Sleep -Milliseconds 500
    } while ([DateTime]::UtcNow -lt $limit)
    throw 'Worker is still busy; no force stop or code replacement performed'
}
$dockerSettings = Join-Path $env:APPDATA 'Docker\settings-store.json'
function Set-Autostart([bool]$Value, $Expected) {
    if (!(Test-Path -LiteralPath $dockerSettings)) { throw 'Docker settings unavailable' }
    $before = [IO.File]::ReadAllBytes($dockerSettings)
    $d = [Text.Encoding]::UTF8.GetString($before).TrimStart([char]0xfeff) | ConvertFrom-Json
    if ($null -ne $Expected -and [bool]$d.autoStart -ne [bool]$Expected) { throw 'Docker AutoStart changed concurrently; not overwritten' }
    $d | Add-Member -NotePropertyName autoStart -NotePropertyValue $Value -Force
    $current = [IO.File]::ReadAllBytes($dockerSettings)
    if ([Convert]::ToBase64String($before) -ne [Convert]::ToBase64String($current)) { throw 'Docker settings drifted; not overwritten' }
    Put-Json $dockerSettings $d
    $read = Get-Content -Encoding UTF8 -LiteralPath $dockerSettings -Raw | ConvertFrom-Json
    if ([bool]$read.autoStart -ne $Value) { throw 'Docker AutoStart read-back differs' }
}
if ($Rollback) {
    if (!$BackupPath) { throw 'BackupPath required' }
    $backup = [IO.Path]::GetFullPath($BackupPath)
    $saved = Get-Content -Encoding UTF8 -LiteralPath (Join-Path $backup 'install-state.json') -Raw | ConvertFrom-Json
    if ($saved.target -ne $target -or $saved.bridgeTask -ne $BridgeTask -or $saved.ollamaTask -ne $OllamaTask -or $saved.watchdogTask -ne $WatchdogTask) { throw 'Backup belongs to another target/task set' }
    # All drift checks precede maintenance, stopping tasks, or any restored file.
    $partial = $saved.phase -eq 'prepared'
    $needStop = !$partial
    foreach ($file in $files) {
        $installed = Join-Path $target $file
        $present = Test-Path -LiteralPath $installed
        $actual = if ($present) { (Get-FileHash -LiteralPath $installed).Hash } else { $null }
        $candidateMatch = $present -and $actual -eq $saved.installedHashes.$file
        $originalMatch = ($present -and $actual -eq $saved.originalHashes.$file) -or (!$present -and !$saved.originalHashes.$file)
        if (!$originalMatch) { $needStop = $true }
        if (!$candidateMatch -and !($partial -and $originalMatch)) { throw ('Rollback file conflict: ' + $file) }
    }
    $configHash = (Get-FileHash -LiteralPath (Join-Path $target 'config.json')).Hash
    if ($configHash -ne $saved.originalConfigHash) { $needStop = $true }
    if ($configHash -ne $saved.installedConfigHash -and !($partial -and $configHash -eq $saved.originalConfigHash)) { throw 'Rollback config conflict' }
    $expectedTasks = if ($partial) { $saved.tasks } else { $saved.installedTasks }
    foreach ($expectedTask in $expectedTasks) {
        $current = Task-State $expectedTask.name
        $matches = $current.exists -eq $expectedTask.exists -and (!$current.exists -or (Task-Equivalent $current.xml $expectedTask.xml))
        if ($partial -and $expectedTask.name -eq $WatchdogTask -and $current.exists) {
            $matches = $matches -or (Task-Equivalent $current.xml $saved.plannedWatchdogXml) -or (Task-Equivalent $current.xml $saved.temporaryWatchdogXml)
        }
        if (!$matches) { throw ('Rollback task conflict: ' + $expectedTask.name) }
    }
    if (!$Candidate) {
        $autoNow = [bool](Get-Content -Encoding UTF8 -LiteralPath $dockerSettings -Raw | ConvertFrom-Json).autoStart
        $autoExpected = if ($partial -and !$saved.dockerIntent) { [bool]$saved.dockerAutostart } else { $true }
        if ($autoNow -ne $autoExpected -and !($partial -and $saved.dockerIntent -and $autoNow -eq [bool]$saved.dockerAutostart)) { throw 'Rollback Docker AutoStart conflict' }
    }
    $startupJournal = Join-Path $backup 'docker-startup.json'
    if (!$Candidate -and (Test-Path -LiteralPath $startupJournal)) { & (Join-Path $sourceRoot 'docker-startup.ps1') -Action VerifyRollback -BackupPath $backup -AllowOriginal:$partial }
    Put-Json (Join-Path $target 'maintenance.json') @{paused=$true}
    $watch = Get-ScheduledTask -TaskName $WatchdogTask -ErrorAction SilentlyContinue
    if ($watch) { Disable-ScheduledTask -TaskName $WatchdogTask | Out-Null; Stop-ScheduledTask -TaskName $WatchdogTask }
    if ($needStop) { Stop-Worker }
    foreach ($file in $files) {
        $original = Join-Path $backup $file
        $installed = Join-Path $target $file
        if (Test-Path -LiteralPath $original) { Copy-Atomic $original $installed }
        elseif (Test-Path -LiteralPath $installed) { Remove-Item -LiteralPath $installed }
    }
    Copy-Atomic (Join-Path $backup 'config.json') (Join-Path $target 'config.json')
    foreach ($taskState in $saved.tasks) {
        if ($taskState.exists) {
            Register-ScheduledTask -TaskName $taskState.name -Xml $taskState.xml -Force | Out-Null
            if (!$taskState.enabled) { Disable-ScheduledTask -TaskName $taskState.name | Out-Null }
        } else { Unregister-ScheduledTask -TaskName $taskState.name -Confirm:$false -ErrorAction SilentlyContinue }
    }
    if (!$Candidate) {
        Set-Autostart $saved.dockerAutostart $autoNow
        if (Test-Path -LiteralPath $startupJournal) { & (Join-Path $sourceRoot 'docker-startup.ps1') -Action Rollback -BackupPath $backup -AllowOriginal:$partial }
    }
    if ($saved.maintenanceExists) { Copy-Item -LiteralPath (Join-Path $backup 'maintenance.json') -Destination (Join-Path $target 'maintenance.json') -Force }
    else { Remove-Item -LiteralPath (Join-Path $target 'maintenance.json') }
    $stopPath = Join-Path $target 'stop.json'
    if ($saved.stopExists) { Copy-Item -LiteralPath (Join-Path $backup 'stop.json') -Destination $stopPath -Force }
    elseif (Test-Path -LiteralPath $stopPath) { Remove-Item -LiteralPath $stopPath }
    foreach ($taskState in $saved.tasks) {
        if ($taskState.exists -and $taskState.running -and $taskState.enabled -and !$saved.maintenancePaused) { Start-ScheduledTask -TaskName $taskState.name }
    }
    @{status='rolled_back';target=$target;queueRestored=$false} | ConvertTo-Json
    exit 0
}
# Verify the snapshot of active source before even a maintenance pause.
$baseline = Get-Content -Encoding UTF8 -LiteralPath (Join-Path $sourceRoot 'baseline-manifest.json') -Raw | ConvertFrom-Json
foreach ($entry in $baseline.PSObject.Properties) {
    $file = Join-Path $target $entry.Name
    if (!(Test-Path -LiteralPath $file) -or (Get-FileHash -LiteralPath $file).Hash -ne $entry.Value) { throw ('Active source drift: ' + $entry.Name) }
}
foreach ($file in $files) { if (!(Test-Path -LiteralPath (Join-Path $sourceRoot $file))) { throw ('Missing package file: ' + $file) } }
if ($DryRun) {
    @{status='dry-run';target=$target;files=$files;tasks=@($BridgeTask,$OllamaTask,$WatchdogTask);liveChanges=$false} | ConvertTo-Json -Depth 3
    exit 0
}
if (!$Apply) { throw 'Apply required' }
if ($sourceRoot -eq $target) { throw 'Source and target cannot be the same' }
if ($InjectFailure -eq 'Manifest') { throw 'Injected manifest failure before switching' }
if (!$Candidate) {
    $runtimeContainers = @{}
    $composeJson = & $cfg.docker compose -f $cfg.compose_file config --format json
    if ($LASTEXITCODE -ne 0) { throw 'Compose config unavailable; manifest not verified' }
    $composeConfig = ConvertFrom-Json -InputObject ($composeJson -join "`n")
    foreach ($item in @(@{name='speakr-local';service='app'},@{name='speakr-whisperx';service='whisperx-asr'})) {
        $imageId = & $cfg.docker inspect --format '{{.Image}}' $item.name
        if ($LASTEXITCODE -ne 0) { throw 'Live image identity unavailable' }
        $ref = $composeConfig.services.($item.service).image
        $refId = & $cfg.docker image inspect --format '{{.Id}}' $ref
        if ($LASTEXITCODE -ne 0 -or $refId -ne $imageId) { throw 'Compose/local image/container mismatch' }
        $runtimeContainers[$item.name] = @{service=$item.service;image_id=$imageId;image_ref=$ref}
    }
    $cfg | Add-Member -NotePropertyName runtime_manifest -NotePropertyValue @{compose_sha256=(Get-FileHash -LiteralPath $cfg.compose_file).Hash.ToLowerInvariant();containers=$runtimeContainers} -Force
}
$scheduler = New-Object -ComObject 'Schedule.Service'
$scheduler.Connect()
$definition = $scheduler.NewTask(0)
$definition.RegistrationInfo.Description = 'Восстановление локального Speakr; maintenance имеет приоритет.'
$definition.RegistrationInfo.URI = '\' + $WatchdogTask
$definition.Principal.UserId = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$definition.Principal.LogonType = 3
$definition.Settings.MultipleInstances = 2
$definition.Settings.ExecutionTimeLimit = 'PT45S'
$definition.Settings.DisallowStartIfOnBatteries = $false
$definition.Settings.StopIfGoingOnBatteries = $false
$definition.Settings.StartWhenAvailable = $true
$logon = $definition.Triggers.Create(9)
$logon.UserId = $definition.Principal.UserId
$timer = $definition.Triggers.Create(1)
$timer.StartBoundary = (Get-Date).AddMinutes(1).ToString('yyyy-MM-ddTHH:mm:ss')
$timer.Repetition.Interval = 'PT1M'
$exec = $definition.Actions.Create(0)
$exec.Path = ($Python -replace 'python.exe$','pythonw.exe')
$exec.Arguments = '"' + (Join-Path $target 'watchdog.py') + '"'
$exec.WorkingDirectory = $target
$plannedWatchdogXml = $definition.XmlText
$backup = Join-Path $target ('backups\recovery-' + [DateTime]::UtcNow.ToString('yyyyMMdd-HHmmss') + '-' + [guid]::NewGuid().ToString('N').Substring(0,8))
New-Item -ItemType Directory -Path $backup -Force | Out-Null
$maintenancePath = Join-Path $target 'maintenance.json'
$maintenanceExists = Test-Path -LiteralPath $maintenancePath
$maintenancePaused = $false
if ($maintenanceExists) {
    Copy-Item -LiteralPath $maintenancePath -Destination $backup
    $maintenancePaused = [bool](Get-Content -Encoding UTF8 -LiteralPath $maintenancePath -Raw | ConvertFrom-Json).paused
}
$stopExists = Test-Path -LiteralPath (Join-Path $target 'stop.json')
if ($stopExists) { Copy-Item -LiteralPath (Join-Path $target 'stop.json') -Destination $backup }
$tasks = @((Task-State $BridgeTask),(Task-State $OllamaTask),(Task-State $WatchdogTask))
$autostart = $false
if (!$Candidate) { $autostart = [bool](Get-Content -Encoding UTF8 -LiteralPath $dockerSettings -Raw | ConvertFrom-Json).autoStart }
$hashes = @{}
$originalHashes = @{}
foreach ($file in $files) {
    $original = Join-Path $target $file
    if (Test-Path -LiteralPath $original) { Copy-Item -LiteralPath $original -Destination $backup; $originalHashes[$file]=(Get-FileHash -LiteralPath $original).Hash }
    $hashes[$file] = (Get-FileHash -LiteralPath (Join-Path $sourceRoot $file)).Hash
}
Copy-Item -LiteralPath (Join-Path $target 'config.json') -Destination $backup
$statePath = Join-Path $backup 'install-state.json'
$temporaryWatchdogXml = $null
if ($tasks[2].exists) {
    $temporary = $scheduler.NewTask(0)
    $temporary.XmlText = $tasks[2].xml
    $temporary.Settings.Enabled = $false
    $temporaryWatchdogXml = $temporary.XmlText
}
$state = @{phase='prepared';target=$target;bridgeTask=$BridgeTask;ollamaTask=$OllamaTask;watchdogTask=$WatchdogTask;
  tasks=$tasks;installedHashes=$hashes;originalHashes=$originalHashes;originalConfigHash=(Get-FileHash -LiteralPath (Join-Path $target 'config.json')).Hash;
  stopExists=$stopExists;maintenanceExists=$maintenanceExists;maintenancePaused=$maintenancePaused;dockerAutostart=$autostart;
  plannedWatchdogXml=$plannedWatchdogXml;temporaryWatchdogXml=$temporaryWatchdogXml}
Put-Json $statePath $state
try {

Put-Json $maintenancePath @{paused=$true}
if ($tasks[2].exists) { Disable-ScheduledTask -TaskName $WatchdogTask | Out-Null; Stop-ScheduledTask -TaskName $WatchdogTask }
Stop-Worker
if ($InjectFailure -eq 'Backup') { throw 'Injected queue backup failure' }
& $Python (Join-Path $sourceRoot 'backup-state.py') --root $target --destination (Join-Path $backup 'state.sqlite3')
if ($LASTEXITCODE -ne 0) { throw 'Queue backup failed; package not installed' }
foreach ($file in $files) {
    Copy-Atomic (Join-Path $sourceRoot $file) (Join-Path $target $file) -FailBeforeReplace:($InjectFailure -eq 'BeforeReplace')
    if ($InjectFailure -eq 'MiddleCopy') { throw 'Injected mid-copy failure' }
}
if ($InjectFailure -eq 'AfterCopy') { throw 'Injected post-copy failure' }
$cfg | Add-Member -NotePropertyName recovery_enabled -NotePropertyValue $true -Force
$legacyJson = & $Python (Join-Path $sourceRoot 'backup-state.py') --root $target --list-failed
if ($LASTEXITCODE -ne 0) { throw 'Legacy queue classification failed' }
$legacyParsed = ConvertFrom-Json -InputObject ($legacyJson -join '')
$legacyIds = @($legacyParsed | ForEach-Object { [string]$_ })
$cfg | Add-Member -NotePropertyName legacy_recovery_jobs -NotePropertyValue $legacyIds -Force
$cfg | Add-Member -NotePropertyName bridge_task -NotePropertyValue $BridgeTask -Force
$cfg | Add-Member -NotePropertyName ollama_task -NotePropertyValue $OllamaTask -Force
$cfg | Add-Member -NotePropertyName watchdog_task -NotePropertyValue $WatchdogTask -Force
$llmUrl = if ($Candidate) {'http://127.0.0.1:1'} else {'http://127.0.0.1:11435'}
$cfg | Add-Member -NotePropertyName llm_url -NotePropertyValue $llmUrl -Force
$cfg | Add-Member -NotePropertyName llm_model -NotePropertyValue 'qwen3.5:9b-speakr' -Force
$cfg | Add-Member -NotePropertyName speakr_health_url -NotePropertyValue ($cfg.speakr_url + '/login') -Force
$asrUrl = if ($Candidate) {'http://127.0.0.1:1/health'} else {'http://127.0.0.1:9000/health'}
$cfg | Add-Member -NotePropertyName asr_health_url -NotePropertyValue $asrUrl -Force
$cfg | Add-Member -NotePropertyName supervision_candidate -NotePropertyValue ([bool]$Candidate) -Force

Put-Json (Join-Path $backup 'planned-config.json') $cfg
$state.installedConfigHash = (Get-FileHash -LiteralPath (Join-Path $backup 'planned-config.json')).Hash
Put-Json $statePath $state
Copy-Atomic (Join-Path $backup 'planned-config.json') (Join-Path $target 'config.json')
New-Item -ItemType Directory -Path (Join-Path $target 'watchdog') -Force | Out-Null
Register-ScheduledTask -TaskName $WatchdogTask -Xml $plannedWatchdogXml -Force | Out-Null
if ($InjectFailure -eq 'AfterTask') { throw 'Injected post-task failure' }
$state.installedTasks = @((Task-State $BridgeTask),(Task-State $OllamaTask),(Task-State $WatchdogTask))
Put-Json $statePath $state
if (!$Candidate) { $state.dockerIntent=$true; Put-Json $statePath $state; Set-Autostart $true $autostart; & (Join-Path $sourceRoot 'docker-startup.ps1') -Action Enable -BackupPath $backup | Out-Null }
# Stay paused for read-back and separate backlog approval. Activation is explicit resume.
$state.phase = 'installed'
Put-Json $statePath $state
@{status='installed_paused';target=$target;backup=$backup;watchdogXml=(Export-ScheduledTask -TaskName $WatchdogTask)} | ConvertTo-Json -Depth 3
} catch {
    $failure = $_
    & $PSCommandPath -TargetRoot $target -Rollback -BackupPath $backup -BridgeTask $BridgeTask -OllamaTask $OllamaTask -WatchdogTask $WatchdogTask -Python $Python -Candidate:$Candidate | Out-Null
    throw $failure
}
