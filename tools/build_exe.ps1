# PowerShell helper to build standalone APA-Tracker.exe
$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Split-Path -Parent $scriptDir
$venvPy = Join-Path $projectRoot "backend\.venv\Scripts\python.exe"

Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host " Building APA-Tracker.exe (Windows Desktop Monitor)      " -ForegroundColor Cyan
Write-Host "==========================================================" -ForegroundColor Cyan

if (Test-Path $venvPy) {
    Write-Host "Using backend virtual environment: $venvPy" -ForegroundColor Green
    & $venvPy (Join-Path $scriptDir "build_exe.py")
} else {
    Write-Host "Backend venv not found, using system Python..." -ForegroundColor Yellow
    python (Join-Path $scriptDir "build_exe.py")
}
