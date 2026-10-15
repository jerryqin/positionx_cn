param(
  [Parameter(Mandatory=$true)][string]$ProjectRoot,
  [Parameter(Mandatory=$true)][string]$DataRoot,
  [ValidateSet("Preview","Apply","Rollback")][string]$Mode="Preview",
  [string]$BackupDir=""
)
$ErrorActionPreference="Stop"
$Registry=Get-Content (Join-Path $ProjectRoot "config\ops\daily_ops_task_registry_v1.json") -Raw | ConvertFrom-Json
$Python=Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$Orchestrator=Join-Path $ProjectRoot "scripts\run_daily_ops_orchestrator_v1.py"
function New-WeekdayTriggers([string[]]$Times){$out=@();foreach($x in $Times){$out+=New-ScheduledTaskTrigger -Weekly -WeeksInterval 1 -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At $x};return $out}
function New-DailyTriggers([string[]]$Times){$out=@();foreach($x in $Times){$out+=New-ScheduledTaskTrigger -Daily -At $x};return $out}
function CmdArgs([string]$command){return "-B `"$Orchestrator`" $command --project-root `"$ProjectRoot`" --data-root `"$DataRoot`" --scheduled-source windows-task"}
$Reference=Get-ScheduledTask -TaskName $Registry.scheduler.reference_task
$Principal=$Reference.Principal
# Deliberately no catch-up; recovery is explicit in task-level retry policies.
$Settings=New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 2) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$Plan=@();foreach($t in $Registry.scheduler.new_tasks){$Plan += [pscustomobject]@{Operation="UPSERT_NEW";TaskName=$t.name;Scope=$t.scope;Times=@($t.times);Command=$t.command}}
$Plan += [pscustomobject]@{Operation="UPDATE_EXISTING";TaskName="PositionX-DeepOversold-CSI500-Forward";Scope="WEEKDAYS";Times=@("15:45","16:00","16:15","16:30");Command="task DEEP_FORWARD"}
if($Mode -eq "Preview"){$Plan|ConvertTo-Json -Depth 5;exit 0}
if($env:POSITIONX_GUARDED_SCHEDULER_APPLY -ne "1"){throw "Apply/Rollback requires POSITIONX_GUARDED_SCHEDULER_APPLY=1"}
if(-not $BackupDir){throw "BackupDir is required"}
New-Item -ItemType Directory -Force -Path $BackupDir|Out-Null
$Manifest=Join-Path $BackupDir "task_prestate.tsv"
if($Mode -eq "Rollback"){
  if(-not (Test-Path $Manifest)){throw "Rollback manifest missing: $Manifest"}
  Get-Content $Manifest|ForEach-Object{
    if(-not $_){return};$parts=$_.Split("`t");$name=$parts[0];$existed=$parts[1] -eq "1";$safe=$name.Replace(" ","__")
    if($existed){$xmlPath=Join-Path $BackupDir "$safe.xml";if(-not (Test-Path $xmlPath)){throw "Rollback XML missing: $xmlPath"};Register-ScheduledTask -TaskName $name -Xml (Get-Content $xmlPath -Raw) -Force|Out-Null}
    else {if(Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue){Unregister-ScheduledTask -TaskName $name -Confirm:$false}}
  };exit 0
}
$Names=@("PositionX-DeepOversold-CSI500-Forward");$Names+=@($Registry.scheduler.new_tasks|ForEach-Object{$_.name});$manifestLines=@()
foreach($name in $Names){$existing=Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue;if($existing){$xml=Export-ScheduledTask -TaskName $name;$safe=$name.Replace(" ","__");Set-Content -Path (Join-Path $BackupDir "$safe.xml") -Value $xml -Encoding UTF8;$manifestLines += "$name`t1"}else{$manifestLines += "$name`t0"}}
Set-Content -Path $Manifest -Value $manifestLines -Encoding UTF8
foreach($t in $Registry.scheduler.new_tasks){$action=New-ScheduledTaskAction -Execute $Python -Argument (CmdArgs $t.command) -WorkingDirectory $ProjectRoot;if($t.scope -eq "DAILY"){$triggers=New-DailyTriggers @($t.times)}else{$triggers=New-WeekdayTriggers @($t.times)};Register-ScheduledTask -TaskName $t.name -Action $action -Trigger $triggers -Principal $Principal -Settings $Settings -Force|Out-Null}
$deepAction=New-ScheduledTaskAction -Execute $Python -Argument (CmdArgs "task DEEP_FORWARD") -WorkingDirectory $ProjectRoot
$deepTriggers=New-WeekdayTriggers @("15:45","16:00","16:15","16:30")
Set-ScheduledTask -TaskName "PositionX-DeepOversold-CSI500-Forward" -Action $deepAction -Trigger $deepTriggers -Settings $Settings|Out-Null
$Plan|ConvertTo-Json -Depth 5
