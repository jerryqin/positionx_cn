param(
    [string]$TaskName = "PositionX Optimus Current22 0830"
)
$ErrorActionPreference="Stop"
$task=Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -eq $task) {
    Write-Host "TASK_NOT_FOUND: $TaskName"
    exit 0
}
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
Write-Host "PASS_OPTIMUS_CURRENT22_TASK_UNINSTALL: $TaskName"
