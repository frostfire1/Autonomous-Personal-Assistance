<#
.SYNOPSIS
    Interactive setup for the APA Laptop Activity Monitor Service on Windows.
.DESCRIPTION
    Configures and installs APA Laptop Watcher as a silent Windows Scheduled Task.
    Reads existing .env file if present for convenience.
#>

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$RepoRoot = Split-Path -Parent $ScriptDir
$EnvFile = Join-Path $RepoRoot ".env"

Write-Host "=====================================================" -ForegroundColor Cyan
Write-Host "  APA Laptop Activity Tracker - Windows Setup" -ForegroundColor Cyan
Write-Host "=====================================================" -ForegroundColor Cyan
Write-Host "This service tracks active applications and screen time on your laptop"
Write-Host "and pushes usage data to your APA server quietly in the background."
Write-Host ""

# Default detection from .env if available
$DefaultServer = "http://localhost:8000"
$DefaultKey = ""

if (Test-Path $EnvFile) {
    Get-Content $EnvFile | ForEach-Object {
        if ($_ -match "^INGEST_API_KEY=(.+)$") {
            $DefaultKey = $matches[1].Trim()
        }
    }
}

$ServerUrl = Read-Host "Enter APA Server URL [default: $DefaultServer]"
if ([string]::IsNullOrWhiteSpace($ServerUrl)) {
    $ServerUrl = $DefaultServer
}

if (![string]::IsNullOrWhiteSpace($DefaultKey)) {
    $ApiKey = Read-Host "Enter INGEST_API_KEY [detected: $DefaultKey]"
    if ([string]::IsNullOrWhiteSpace($ApiKey)) {
        $ApiKey = $DefaultKey
    }
} else {
    $ApiKey = Read-Host "Enter INGEST_API_KEY"
}

if ([string]::IsNullOrWhiteSpace($ApiKey)) {
    Write-Host "Error: INGEST_API_KEY cannot be empty!" -ForegroundColor Red
    exit 1
}

$DeviceName = Read-Host "Enter Device Name [default: $env:COMPUTERNAME]"
if ([string]::IsNullOrWhiteSpace($DeviceName)) {
    $DeviceName = $env:COMPUTERNAME
}

Write-Host ""
Write-Host "Configuring service with:" -ForegroundColor Yellow
Write-Host "  Server : $ServerUrl"
Write-Host "  Key    : $ApiKey"
Write-Host "  Device : $DeviceName"
Write-Host ""

# Test server connectivity
Write-Host "Testing connectivity to APA server..." -NoNewline
try {
    $test = Invoke-WebRequest -Uri "$ServerUrl/healthz" -TimeoutSec 3 -ErrorAction SilentlyContinue
    Write-Host " [OK]" -ForegroundColor Green
} catch {
    Write-Host " [Notice: Server not responding right now, but will connect once server is up]" -ForegroundColor Yellow
}

$WatcherScript = Join-Path $ScriptDir "laptop_watcher.py"
& python "$WatcherScript" --server "$ServerUrl" --key "$ApiKey" --device "$DeviceName" --install-service

Write-Host ""
Write-Host "Setup finished! You can check the service anytime with:" -ForegroundColor Green
Write-Host "  python tools\laptop_watcher.py --status" -ForegroundColor White
Write-Host "  python tools\laptop_watcher.py --stop" -ForegroundColor White
Write-Host "  python tools\laptop_watcher.py --start" -ForegroundColor White
Write-Host ""
