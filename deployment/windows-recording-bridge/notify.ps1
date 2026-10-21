param([ValidateSet('Outage','Recovered')][string]$Event, [switch]$TestMode, [string]$EvidencePath)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$icon = [Windows.Forms.NotifyIcon]::new()
$result = @{issued=$false;shown=$false;testMode=[bool]$TestMode}
try {
    $icon.Icon = [Drawing.SystemIcons]::Information
    $icon.Visible = $true
    $body = if ($Event -eq 'Recovered') {'Сервисы снова доступны. Обработка очереди продолжается.'} else {'Обработка записей задерживается более 10 минут. Проверьте статус Speakr.'}
    if ($TestMode) { $body = 'Проверка уведомлений кандидата. Рабочая очередь не менялась.' }
    $icon.add_BalloonTipShown([EventHandler]{ $result.shown = $true })
    $icon.ShowBalloonTip(8000, 'Speakr: обработка записей', $body, [Windows.Forms.ToolTipIcon]::Info)
    $result.issued = $true
    $deadline = [DateTime]::UtcNow.AddSeconds(9)
    do { [Windows.Forms.Application]::DoEvents(); Start-Sleep -Milliseconds 50 } while ([DateTime]::UtcNow -lt $deadline)
} finally {
    $icon.Dispose()
    if ($EvidencePath) { $result | ConvertTo-Json | Set-Content -LiteralPath $EvidencePath -Encoding UTF8 }
}
