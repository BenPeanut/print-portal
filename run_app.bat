@echo off
setlocal
set SCRIPT_DIR=%~dp0
pushd "%SCRIPT_DIR%"

if exist "dist\PrintingBusinessApp.exe" (
    echo Launching built PrintingBusinessApp.exe...
    start "" "%SCRIPT_DIR%dist\PrintingBusinessApp.exe" --open-path /desktop-capture --port 5077
    popd
    exit /b 0
)

if exist ".venv\Scripts\python.exe" (
    echo Launching source app from .venv...
    call ".venv\Scripts\python.exe" "%SCRIPT_DIR%main.py" --open-path /desktop-capture --port 5077
    popd
    exit /b 0
)

echo ERROR: No runnable app found.
echo 1) Build the exe with build_webview_exe.ps1
echo 2) Or create a Python virtual environment in .venv and install requirements
pause
popd
exit /b 1
