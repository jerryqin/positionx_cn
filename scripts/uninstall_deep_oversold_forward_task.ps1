param(
    [string]$TaskName = "PositionX-DeepOversold-CSI500-Forward"
)

$ErrorActionPreference = "Stop"

$Task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($Task) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "REMOVED scheduled task: $TaskName"
} else {
    Write-Host "Task not found: $TaskName"
}
Write-Host "Forward DB/state/log files were NOT deleted."
