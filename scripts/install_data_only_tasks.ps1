param(
    [Parameter(Mandatory=$true)][ValidateSet('preopen','postclose')][string]$Phase,
    [switch]$Enable,
    [switch]$Rollback,
    [string]$BackupPath
)
$ErrorActionPreference = 'Stop'
$projectRoot = 'D:\positionx_cn'
$opsRoot = 'D:\positionx_data\ops\three_strategy_data_v1'
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
$entryPath = Join-Path $projectRoot 'scripts\run_data_only_daily.py'
$taskName = if ($Phase -eq 'preopen') { 'PositionX-DataOnly-Preopen' } else { 'PositionX-DataOnly-Postclose' }
if ($Rollback) {
    if (-not $BackupPath) { throw 'EXACT_THIS_APPLICATION_BACKUP_REQUIRED' }
    $backup = Get-Content -LiteralPath $BackupPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($backup.schema -ne 'RQDATA_DATA_ONLY_TASK_BACKUP_V1' -or $backup.task_name -ne $taskName -or -not $backup.xml) { throw 'BACKUP_TASK_SCOPE_MISMATCH' }
    Register-ScheduledTask -TaskName $taskName -TaskPath '\' -Xml $backup.xml -Force | Out-Null
    if ($backup.state -eq 'Disabled') { Disable-ScheduledTask -TaskName $taskName -TaskPath '\' | Out-Null }
    Get-ScheduledTask -TaskName $taskName -TaskPath '\' | Select-Object TaskName,State
    exit 0
}
if ($BackupPath) { throw 'BACKUP_PATH_IS_ONLY_FOR_EXACT_ROLLBACK' }
if (-not (Test-Path -LiteralPath $pythonPath) -or -not (Test-Path -LiteralPath $entryPath)) { throw 'DATA_ENTRY_MISSING' }
if ((Get-TimeZone).Id -ne 'China Standard Time') { throw 'ASIA_SHANGHAI_TASK_TIMEZONE_REQUIRED' }
$existing = Get-ScheduledTask -TaskName $taskName -TaskPath '\' -ErrorAction Stop
if ($existing.State -eq 'Running') { throw 'SCOPED_TASK_IS_RUNNING' }
$backupRoot = Join-Path $opsRoot 'task_backups'
New-Item -ItemType Directory -Path $backupRoot -Force | Out-Null
$backupPath = Join-Path $backupRoot ($Phase+'_'+(Get-Date -Format 'yyyyMMddTHHmmssfff')+'_'+[guid]::NewGuid().ToString('N').Substring(0,8)+'.json')
@{ schema='RQDATA_DATA_ONLY_TASK_BACKUP_V1'; task_name=$taskName; phase=$Phase;
   captured_at=(Get-Date -Format o); state=[string]$existing.State;
   xml=(Export-ScheduledTask -TaskName $taskName -TaskPath '\') } |
    ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $backupPath -Encoding UTF8
$principal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 5) -StartWhenAvailable -WakeToRun -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$times = if ($Phase -eq 'preopen') { @('08:20','09:00','09:10') } else { @('16:10','16:40','17:10','17:15') }
$triggers = @($times | ForEach-Object { New-ScheduledTaskTrigger -Daily -At $_ })
$action = New-ScheduledTaskAction -Execute $pythonPath -Argument ('-X utf8 -B "'+$entryPath+'" --phase '+$Phase) -WorkingDirectory $projectRoot
# Installation is disabled unless this specific phase has passed its actual HTTPS
# method, return-contract and entry checks and the caller explicitly uses -Enable.
$task = New-ScheduledTask -Action $action -Trigger $triggers -Settings $settings -Principal $principal -Description 'RQData HTTPS data and local Deep/RET120 research settlement/report. No Broker, orders or formal Forward. Exact-day/cutoff checks are in the entry.'
$task.Settings.Enabled = [bool]$Enable
Register-ScheduledTask -TaskName $taskName -TaskPath '\' -InputObject $task -Force | Out-Null
if ($Enable) { Enable-ScheduledTask -TaskName $taskName -TaskPath '\' | Out-Null }
else { Disable-ScheduledTask -TaskName $taskName -TaskPath '\' | Out-Null }
$installed = Get-ScheduledTask -TaskName $taskName -TaskPath '\'
$info = Get-ScheduledTaskInfo -TaskName $taskName -TaskPath '\'
[pscustomobject]@{TaskName=$installed.TaskName;State=[string]$installed.State;BackupPath=$backupPath;
                 NextRunTime=$info.NextRunTime;LastTaskResult=$info.LastTaskResult;Entry=$entryPath} | ConvertTo-Json -Depth 3
