param(
 [ValidateSet('Enable','VerifyRollback','Rollback')][string]$Action,
 [Parameter(Mandatory=$true)][string]$BackupPath,
 [switch]$AllowOriginal,
 [string]$RegistryPath = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run',
 [switch]$Candidate,
 [switch]$FailBeforeWrite
)
$ErrorActionPreference='Stop'
$livePath='HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run'
if ($Candidate -and $RegistryPath -notlike 'HKCU:\Software\CodexSpeakrCandidate-*') { throw 'Candidate registry must be isolated' }
if (!$Candidate -and $RegistryPath -ne $livePath) { throw 'Only Docker startup approval is supported' }
if ($FailBeforeWrite -and !$Candidate) { throw 'Failure injection is candidate-only' }
$name='Docker Desktop'
$journal=Join-Path $BackupPath 'docker-startup.json'
function Save-Journal($saved) {
 $tmp=$journal+'.new-'+[guid]::NewGuid().ToString('N')
 $saved | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $tmp -Encoding UTF8
 Move-Item -LiteralPath $tmp -Destination $journal -Force
}
function Read-Value {
 $entry=Get-ItemProperty -LiteralPath $RegistryPath -Name $name -ErrorAction SilentlyContinue
 if ($entry) { return @{exists=$true;bytes=@($entry.$name)} }
 return @{exists=$false;bytes=@()}
}
function Same($a,$b) { $a.exists -eq $b.exists -and (@($a.bytes) -join ',') -eq (@($b.bytes) -join ',') }
if ($Action -eq 'Enable') {
 if (Test-Path -LiteralPath $journal) { throw 'Startup journal already exists' }
 if (!$Candidate) {
   $command=(Get-ItemProperty -LiteralPath 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -Name $name).$name
   if ($command.Trim('"') -ne 'C:\Program Files\Docker\Docker\Docker Desktop.exe') { throw 'Docker startup command differs; not modified' }
 }
 $original=Read-Value
 $installed=@{exists=$original.exists;bytes=@($original.bytes)}
 if ($original.exists) {
   if ($original.bytes.Count -ne 12 -or $original.bytes[0] -notin @(2,3)) { throw 'Unknown Windows startup approval; not modified' }
   $installed.bytes[0]=2
 }
 $saved=@{phase='intent';registryPath=$RegistryPath;name=$name;original=$original;installed=$installed}
 Save-Journal $saved
 if (!(Same (Read-Value) $original)) { throw 'Startup approval drift before enable' }
 if ($FailBeforeWrite) { throw 'Injected startup failure before registry write' }
 if ($installed.exists) { Set-ItemProperty -LiteralPath $RegistryPath -Name $name -Value ([byte[]]$installed.bytes) }
 if (!(Same (Read-Value) $installed)) { throw 'Startup approval read-back failed' }
 $saved.phase='applied'
 Save-Journal $saved
 @{enabled=$true;changed=!(Same $original $installed)} | ConvertTo-Json
 exit 0
}
$saved=Get-Content -Encoding UTF8 -Raw -LiteralPath $journal | ConvertFrom-Json
if ($saved.registryPath -ne $RegistryPath -or $saved.name -ne $name) { throw 'Startup journal target differs' }
$current=Read-Value
if ($saved.phase -notin @('intent','applied')) { throw 'Unknown startup journal phase' }
if (!(Same $current $saved.installed) -and !(($AllowOriginal -or $saved.phase -eq 'intent') -and (Same $current $saved.original))) { throw 'Rollback Docker startup approval conflict' }
if ($Action -eq 'Rollback') {
 if ($saved.original.exists) { Set-ItemProperty -LiteralPath $RegistryPath -Name $name -Value ([byte[]]$saved.original.bytes) }
 elseif ($current.exists) { Remove-ItemProperty -LiteralPath $RegistryPath -Name $name }
 if (!(Same (Read-Value) $saved.original)) { throw 'Startup rollback read-back failed' }
}
