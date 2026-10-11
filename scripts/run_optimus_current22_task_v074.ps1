param(
    [string]$ProjectRoot = "D:\positionx_cn"
)

$ErrorActionPreference = "Stop"
Set-Location $ProjectRoot

$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    Write-Error "PositionX venv python not found: $Python"
    exit 10
}

$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"

& $Python "scripts\run_optimus_current22_scheduled_v074.py" run `
    --min-overlap-days 2 `
    --max-catchup-days 30

exit $LASTEXITCODE
