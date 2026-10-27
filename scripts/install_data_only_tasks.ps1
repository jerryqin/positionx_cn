param([switch]$Rollback)
$ErrorActionPreference = 'Stop'
$projectRoot = 'D:\positionx_cn'
$opsRoot = 'D:\positionx_data\ops\three_strategy_data_v1'
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
$entryPath = Join-Path $projectRoot 'scripts\run_data_only_daily.py'
$taskNames = @('PositionX-DataOnly-Preopen','PositionX-DataOnly-Postclose')
$backupPath = Join-Path $opsRoot 'task_definitions_before.json'
New-Item -ItemType Directory -Path $opsRoot -Force | Out-Null
if ($Rollback) {
    $backups = Get-Content -LiteralPath $backupPath -Raw | ConvertFrom-Json
    foreach ($name in $taskNames) {
        $xml = $backups.$name
        if ($xml) { Register-ScheduledTask -TaskName $name -Xml $xml -Force | Out-Null }
        else { Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue }
    }
    exit 0
}
if (-not (Test-Path -LiteralPath $pythonPath) -or -not (Test-Path -LiteralPath $entryPath)) { throw 'DATA_ENTRY_MISSING' }
if ((Get-TimeZone).Id -ne 'China Standard Time') { throw 'ASIA_SHANGHAI_TASK_TIMEZONE_REQUIRED' }
if (-not (Test-Path -LiteralPath $backupPath)) {
    $backups = @{}
    foreach ($name in $taskNames) {
        $task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
        $backups[$name] = if ($task) { Export-ScheduledTask -TaskName $name } else { $null }
    }
    $backups | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $backupPath -Encoding UTF8
}
$principal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 5) -StartWhenAvailable -WakeToRun -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
foreach ($phase in @('preopen','postclose')) {
    $name = if ($phase -eq 'preopen') { $taskNames[0] } else { $taskNames[1] }
    $times = if ($phase -eq 'preopen') { @('08:20','09:00','09:10') } else { @('16:10','16:40','17:10','17:15') }
    $triggers = @($times | ForEach-Object { New-ScheduledTaskTrigger -Daily -At $_ })
    $action = New-ScheduledTaskAction -Execute $pythonPath -Argument ('-X utf8 -B "'+$entryPath+'" --phase '+$phase) -WorkingDirectory $projectRoot
    Register-ScheduledTask -TaskName $name -Action $action -Trigger $triggers -Settings $settings -Principal $principal -Description 'PositionX research data only; no Broker, orders or model ledger actions. Trading-day and cutoff checks are in the entry.' -Force | Out-Null
    Enable-ScheduledTask -TaskName $name | Out-Null
}
Get-ScheduledTask -TaskName $taskNames | Select-Object TaskName,State
