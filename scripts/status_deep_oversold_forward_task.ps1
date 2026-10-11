param(
    [string]$TaskName = "PositionX-DeepOversold-CSI500-Forward"
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir
$State = Join-Path $ProjectRoot "data\research\forward_deep_oversold_csi500_v1_1\state.json"
$Latest = Join-Path $ProjectRoot "data\research\forward_deep_oversold_csi500_v1_1\latest.json"
$LogDir = Join-Path $ProjectRoot "logs\forward_deep_oversold_scheduler"

Write-Host "=== WINDOWS TASK ==="
Get-ScheduledTask -TaskName $TaskName | Format-List TaskName,State
Get-ScheduledTaskInfo -TaskName $TaskName |
    Format-List LastRunTime,LastTaskResult,NextRunTime,NumberOfMissedRuns

Write-Host ""
Write-Host "=== FORWARD STATE ==="
if (Test-Path $Latest) {
    Get-Content $Latest -Raw
} elseif (Test-Path $State) {
    Get-Content $State -Raw
} else {
    Write-Host "Forward state file not found."
}

Write-Host ""
Write-Host "=== LATEST SCHEDULER LOG ==="
$LastLog = Get-ChildItem $LogDir -Filter "*.log" -File -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 1
if ($LastLog) {
    Write-Host $LastLog.FullName
    Get-Content $LastLog.FullName -Tail 80
} else {
    Write-Host "No scheduler log yet."
}


Write-Host ""
Write-Host "=== LATEST FORWARD BACKUP ==="
$BackupRoot = "D:\positionx_data\backup\positionx_forward"
$LastBackup = Get-ChildItem $BackupRoot -Directory -ErrorAction SilentlyContinue |
    Where-Object { Test-Path (Join-Path $_.FullName "manifest.json") } |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 1
if ($LastBackup) {
    Write-Host $LastBackup.FullName
    Get-Content (Join-Path $LastBackup.FullName "manifest.json") -Raw
} else {
    Write-Host "No Forward backup yet."
}
