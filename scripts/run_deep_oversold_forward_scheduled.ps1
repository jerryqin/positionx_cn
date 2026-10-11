param(
    [int]$MaxAttempts = 4,
    [int]$RetrySeconds = 900
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$Runner = Join-Path $ProjectRoot "scripts\run_deep_oversold_csi500_forward.py"
$Backup = Join-Path $ProjectRoot "scripts\backup_deep_oversold_forward.py"
$LogDir = Join-Path $ProjectRoot "logs\forward_deep_oversold_scheduler"

if (-not (Test-Path $Python)) {
    throw "Python not found: $Python"
}
if (-not (Test-Path $Runner)) {
    throw "Forward runner not found: $Runner"
}

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

# Keep scheduler logs useful without growing forever.
Get-ChildItem $LogDir -Filter "*.log" -File -ErrorAction SilentlyContinue |
    Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-120) } |
    Remove-Item -Force -ErrorAction SilentlyContinue

$Stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$Log = Join-Path $LogDir "forward_$Stamp.log"

function Write-Log([string]$Message) {
    $Line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $Message"
    $Line | Tee-Object -FilePath $Log -Append
}

Write-Log "START PositionX Deep Oversold CSI500 Forward scheduled run"
Write-Log "ProjectRoot=$ProjectRoot"
Write-Log "Python=$Python"
Write-Log "Runner=$Runner"

for ($Attempt = 1; $Attempt -le $MaxAttempts; $Attempt++) {
    Write-Log "ATTEMPT $Attempt/$MaxAttempts"

    Push-Location $ProjectRoot
    try {
        & $Python $Runner 2>&1 |
            ForEach-Object {
                $_.ToString() | Tee-Object -FilePath $Log -Append
            }
        $Code = $LASTEXITCODE
    }
    finally {
        Pop-Location
    }

    if ($Code -eq 0) {
        Write-Log "FORWARD_SUCCESS exit_code=0"
        if (-not (Test-Path $Backup)) {
            Write-Log "BACKUP_FAILED missing_script=$Backup"
            $Code = 2
        } else {
            & $Python $Backup --only-if-session-today --scheduler-log $Log 2>&1 |
                ForEach-Object { $_.ToString() | Tee-Object -FilePath $Log -Append }
            $BackupCode = $LASTEXITCODE
            if ($BackupCode -eq 0) {
                Write-Log "SUCCESS forward=0 backup=0"
                exit 0
            }
            Write-Log "BACKUP_FAILED exit_code=$BackupCode"
            $Code = $BackupCode
        }
    }

    Write-Log "FAILED exit_code=$Code"
    if ($Attempt -lt $MaxAttempts) {
        Write-Log "Retrying after $RetrySeconds seconds"
        Start-Sleep -Seconds $RetrySeconds
    }
}

Write-Log "FINAL FAILURE after $MaxAttempts attempts"
exit 1
