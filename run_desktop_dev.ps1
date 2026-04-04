$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $projectRoot

$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) {
    throw 'Expected virtual environment Python at .venv\Scripts\python.exe'
}

# Avoid stale processes from old test runs.
Stop-Process -Name python,PrintingBusinessApp -Force -ErrorAction SilentlyContinue

# Fast loop: run from source without rebuilding an EXE.
& $python main.py --open-path /desktop-capture --port 5077
