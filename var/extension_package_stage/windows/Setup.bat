@echo off
:: MakerWorld Extension - One-Click Setup
:: Double-click this file to configure the local backend and register auto-start.
:: Requirements: Windows 10+, extracted bundle folder, DATABASE_URL from your administrator.

cd /d "%~dp0"

echo ============================================
echo  MakerWorld Extension Setup
echo ============================================
echo.
echo Preparing files (removing Windows download block flags)...
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "Get-ChildItem -Path '%~dp0' -Recurse -File -ErrorAction SilentlyContinue ^| ForEach-Object { Unblock-File -Path $_.FullName -ErrorAction SilentlyContinue }"

echo.
echo Starting bootstrap...
echo.

powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Normal -File "%~dp0bootstrap_extension_setup.ps1"

if %errorlevel% neq 0 (
    echo.
    echo ERROR: Bootstrap failed. See error above.
    pause
    exit /b 1
)

exit /b 0
