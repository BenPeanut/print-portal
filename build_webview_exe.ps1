$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $projectRoot

$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) {
    throw 'Expected virtual environment Python at .venv\Scripts\python.exe'
}

& $python -m pip install --upgrade pip pyinstaller pywebview

$distRoot = Join-Path $projectRoot 'dist'
$buildRoot = Join-Path $projectRoot 'build'
$appName = 'PrintingBusinessApp'
$exePath = Join-Path $distRoot "$appName.exe"

# Stop any running instance so the exe is not file-locked during rebuild.
$runningApp = Get-Process -Name $appName -ErrorAction SilentlyContinue
if ($runningApp) {
    Write-Host "Stopping running $appName process..." -ForegroundColor Yellow
    $runningApp | Stop-Process -Force
    Start-Sleep -Milliseconds 600
}

if (Test-Path $exePath) {
    try {
        Remove-Item $exePath -Force
    }
    catch {
        throw "Could not remove $exePath. Close any open $appName window and try again."
    }
}

if (Test-Path $buildRoot) {
    Remove-Item $buildRoot -Recurse -Force
}

& $python -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --name $appName `
    --hidden-import cloudscraper `
    --hidden-import playwright.sync_api `
    --add-data "templates;templates" `
    --add-data "static;static" `
    --add-data "model_capture_app;model_capture_app" `
    main.py

$envFile = Join-Path $projectRoot '.env'
if (Test-Path $envFile) {
    Copy-Item $envFile (Join-Path $distRoot '.env') -Force
}

Write-Host ''
Write-Host "Build complete: $exePath" -ForegroundColor Green
Write-Host "Run: $exePath" -ForegroundColor Green