param(
    [string]$TaskName = "PositionX-DeepOversold-CSI500-Forward",
    [string]$At = "15:15"
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir
$Wrapper = Join-Path $ProjectRoot "scripts\run_deep_oversold_forward_scheduled.ps1"
$Runner = Join-Path $ProjectRoot "scripts\run_deep_oversold_csi500_forward.py"
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

foreach ($p in @($Wrapper, $Runner, $Python)) {
    if (-not (Test-Path $p)) {
        throw "Required file missing: $p"
    }
}

$Action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$Wrapper`""

$Trigger = New-ScheduledTaskTrigger `
    -Weekly `
    -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday `
    -At $At

# miniQMT/xtdata is an interactive local service in this installation.
# Therefore this task deliberately runs only in the logged-on user's session.
$Principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel Limited

$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Principal $Principal `
    -Settings $Settings `
    -Description "PositionX frozen CSI500 Deep Oversold true-forward collector. Mon-Fri after A-share close. Idempotent; wrapper retries temporary failures." `
    -Force | Out-Null

Write-Host "INSTALLED scheduled task: $TaskName"
Write-Host "Schedule: Monday-Friday $At"
Write-Host "Runs only while this Windows user is logged on."
Write-Host "Wrapper retry policy: 4 attempts, 15 minutes apart."
Write-Host "Holiday runs are harmless no-ops because the Forward runner processes only new completed market sessions."
Write-Host ""
Write-Host "Verify:"
Write-Host "  Get-ScheduledTask -TaskName `"$TaskName`" | Format-List TaskName,State"
Write-Host "  Get-ScheduledTaskInfo -TaskName `"$TaskName`" | Format-List *"
