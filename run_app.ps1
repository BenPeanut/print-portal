$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $projectRoot

$distExe = Join-Path $projectRoot 'dist\PrintingBusinessApp.exe'
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
$sourceMain = Join-Path $projectRoot 'main.py'

if (Test-Path $distExe) {
    Write-Host 'Launching built PrintingBusinessApp.exe...'
    Start-Process -FilePath $distExe -ArgumentList '--open-path', '/desktop-capture', '--port', '5077'
    return
}

if ((Test-Path $python) -and (Test-Path $sourceMain)) {
    Write-Host 'Launching source app from .venv...'
    & $python $sourceMain --open-path /desktop-capture --port 5077
    return
}

Write-Host 'ERROR: No runnable app found.'
Write-Host 'Build the exe with build_webview_exe.ps1 or install requirements into .venv.'
