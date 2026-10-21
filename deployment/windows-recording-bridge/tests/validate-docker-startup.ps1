$ErrorActionPreference='Stop'
$package=Split-Path $PSScriptRoot -Parent
$key='HKCU:\Software\CodexSpeakrCandidate-'+[guid]::NewGuid().ToString('N')
$root=Join-Path $env:TEMP ('speakr-startup-'+[guid]::NewGuid().ToString('N'))
New-Item -Path $key | Out-Null
New-Item -ItemType Directory -Path $root | Out-Null
try {
 $disabled=[byte[]]@(3,0,0,0,0,0,0,0,0,0,0,0)
 New-ItemProperty -LiteralPath $key -Name 'Docker Desktop' -PropertyType Binary -Value $disabled | Out-Null
 & (Join-Path $package 'docker-startup.ps1') -Action Enable -BackupPath $root -Candidate -RegistryPath $key | Out-Null
 $value=(Get-ItemProperty -LiteralPath $key).'Docker Desktop'
 if ($value[0] -ne 2) { throw 'Enable failed' }
 $drift=[byte[]]@(4,0,0,0,0,0,0,0,0,0,0,0)
 Set-ItemProperty -LiteralPath $key -Name 'Docker Desktop' -Value $drift
 $refused=$false
 try { & (Join-Path $package 'docker-startup.ps1') -Action VerifyRollback -BackupPath $root -Candidate -RegistryPath $key }
 catch { $refused=$_.Exception.Message -match 'conflict' }
 if (!$refused -or (Get-ItemProperty -LiteralPath $key).'Docker Desktop'[0] -ne 4) { throw 'Drift was overwritten' }
 Set-ItemProperty -LiteralPath $key -Name 'Docker Desktop' -Value ([byte[]]@(2,0,0,0,0,0,0,0,0,0,0,0))
 & (Join-Path $package 'docker-startup.ps1') -Action Rollback -BackupPath $root -Candidate -RegistryPath $key
 if (((Get-ItemProperty -LiteralPath $key).'Docker Desktop' -join ',') -ne ($disabled -join ',')) { throw 'Original bytes not restored' }
 # A crash between persisted intent and write is also reversible.
 & (Join-Path $package 'docker-startup.ps1') -Action VerifyRollback -BackupPath $root -Candidate -RegistryPath $key -AllowOriginal
 $intent=Join-Path $root 'intent'; New-Item -ItemType Directory -Path $intent | Out-Null
 @{phase='installed'} | ConvertTo-Json | Set-Content -Encoding UTF8 -LiteralPath (Join-Path $intent 'install-state.json')
 $injected=$false
 try { & (Join-Path $package 'docker-startup.ps1') -Action Enable -BackupPath $intent -Candidate -RegistryPath $key -FailBeforeWrite }
 catch { $injected=$_.Exception.Message -match 'Injected' }
 if (!$injected) { throw 'Missing expected intent failure' }
 # Same call used by installer with phase=installed: no AllowOriginal override.
 & (Join-Path $package 'docker-startup.ps1') -Action VerifyRollback -BackupPath $intent -Candidate -RegistryPath $key
 & (Join-Path $package 'docker-startup.ps1') -Action Rollback -BackupPath $intent -Candidate -RegistryPath $key
 $second=Join-Path $root 'absent'; New-Item -ItemType Directory -Path $second | Out-Null
 Remove-ItemProperty -LiteralPath $key -Name 'Docker Desktop'
 & (Join-Path $package 'docker-startup.ps1') -Action Enable -BackupPath $second -Candidate -RegistryPath $key | Out-Null
 & (Join-Path $package 'docker-startup.ps1') -Action Rollback -BackupPath $second -Candidate -RegistryPath $key
 if (Get-ItemProperty -LiteralPath $key -Name 'Docker Desktop' -ErrorAction SilentlyContinue) { throw 'Absent value was invented' }
 @{status='PASS';enableReadback=$true;driftProtected=$true;originalBytesRestored=$true;intentBeforeWrite=$true;installedBackupIntent=$true;absencePreserved=$true;liveRegistryChanged=$false} | ConvertTo-Json | Set-Content -Encoding UTF8 -LiteralPath (Join-Path $package 'evidence/docker-startup-test.json')
} finally {
 if ($key -notmatch '^HKCU:\\Software\\CodexSpeakrCandidate-[0-9a-f]{32}$') { throw 'Cleanup path is outside candidate registry' }
 Remove-Item -LiteralPath $key
}
