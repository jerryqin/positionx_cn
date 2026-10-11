param(
    [string]$ProjectRoot = "D:\positionx_cn",
    [string]$TaskName = "PositionX Optimus Current22 0830",
    [string]$At = "08:30"
)

$ErrorActionPreference = "Stop"

$Runner = Join-Path $ProjectRoot "scripts\run_optimus_current22_task_v074.ps1"
if (-not (Test-Path $Runner)) {
    throw "Task runner missing: $Runner"
}

$User = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

$Action = New-ScheduledTaskAction `
    -Execute "PowerShell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$Runner`" -ProjectRoot `"$ProjectRoot`""

$Trigger = New-ScheduledTaskTrigger -Daily -At $At

$Settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -RestartCount 8 `
    -RestartInterval (New-TimeSpan -Minutes 30) `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
    -MultipleInstances IgnoreNew `
    -RunOnlyIfNetworkAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries

$Principal = New-ScheduledTaskPrincipal `
    -UserId $User `
    -LogonType Interactive `
    -RunLevel Limited

$Task = New-ScheduledTask `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Principal $Principal `
    -Description "PositionX Optimus V0.7.4 Current-22 official monitoring. Daily 08:30; start when available after missed schedule; retry failures every 30 min up to 8 times. Research-only."

Register-ScheduledTask -TaskName $TaskName -InputObject $Task -Force | Out-Null

Write-Host "PASS_OPTIMUS_CURRENT22_TASK_INSTALL"
Write-Host "TaskName: $TaskName"
Write-Host "Schedule: daily $At"
Write-Host "StartWhenAvailable: true"
Write-Host "RestartIntervalMinutes: 30"
Write-Host "RestartCount: 8"
Write-Host "DynamicCatchup: min 2 days, max 30 days"
Write-Host "RunOnlyIfNetworkAvailable: true"
Write-Host "RunLevel: Limited"
Write-Host "RegistryMutation: false"
