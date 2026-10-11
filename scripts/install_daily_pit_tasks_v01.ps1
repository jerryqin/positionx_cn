param(
    [string]$Root = "D:\positionx_min_live",
    [string]$PythonExe = "D:\positionx_min_live\.venv\Scripts\python.exe"
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path $PythonExe)) {
    throw "Python not found: $PythonExe"
}

$Script = Join-Path $Root "scripts\daily_pit_intake_v01.py"
if (-not (Test-Path $Script)) {
    throw "Daily PIT script not found: $Script"
}

$User = "$env:USERDOMAIN\$env:USERNAME"
$Principal = New-ScheduledTaskPrincipal `
    -UserId $User `
    -LogonType S4U `
    -RunLevel Limited

$Settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 15) `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2)

# PREOPEN: weekdays 08:30.
$ActionPre = New-ScheduledTaskAction `
    -Execute $PythonExe `
    -Argument "`"$Script`" --phase preopen" `
    -WorkingDirectory $Root

$TriggerPre = New-ScheduledTaskTrigger `
    -Weekly `
    -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday `
    -At 08:30

Register-ScheduledTask `
    -TaskName "PositionX-DailyPIT-Preopen" `
    -Action $ActionPre `
    -Trigger $TriggerPre `
    -Principal $Principal `
    -Settings $Settings `
    -Force | Out-Null

# POSTCLOSE: every day 18:00.
# Weekend capture preserves first_seen_at for weekend announcements.
$ActionPost = New-ScheduledTaskAction `
    -Execute $PythonExe `
    -Argument "`"$Script`" --phase postclose" `
    -WorkingDirectory $Root

$TriggerPost = New-ScheduledTaskTrigger -Daily -At 18:00

Register-ScheduledTask `
    -TaskName "PositionX-DailyPIT-Postclose" `
    -Action $ActionPost `
    -Trigger $TriggerPost `
    -Principal $Principal `
    -Settings $Settings `
    -Force | Out-Null

Write-Host "Created:"
Write-Host "  PositionX-DailyPIT-Preopen   weekdays 08:30"
Write-Host "  PositionX-DailyPIT-Postclose daily    18:00"
Write-Host ""
Write-Host "IMPORTANT: tasks must run as the same Windows user that owns ~/.iwaos."
Write-Host "Run scripts\smoke_test_iwa_daily_pit_v01.py in the PositionX venv first."
