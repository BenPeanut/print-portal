$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $projectRoot

$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) {
    throw 'Expected virtual environment Python at .venv\Scripts\python.exe'
}

& $python -m pip install --upgrade pip pyinstaller

$distRoot = Join-Path $projectRoot 'dist'
$buildRoot = Join-Path $projectRoot 'build'
$appName = 'PrintingBusinessLauncher'
$outputRoot = Join-Path $distRoot $appName

if (Test-Path $outputRoot) {
    Remove-Item $outputRoot -Recurse -Force
}

if (Test-Path $buildRoot) {
    Remove-Item $buildRoot -Recurse -Force
}

& $python -m PyInstaller `
    --noconfirm `
    --clean `
    --onedir `
    --name $appName `
    --add-data "templates;templates" `
    --add-data "static;static" `
    --add-data "model_capture_app;model_capture_app" `
    desktop_launcher.py

$envFile = Join-Path $projectRoot '.env'
if (Test-Path $envFile) {
    Copy-Item $envFile (Join-Path $outputRoot '.env') -Force
}

Write-Host ''
Write-Host "Build complete: $outputRoot" -ForegroundColor Green
Write-Host "Run: $outputRoot\\$appName.exe" -ForegroundColor Green