$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $projectRoot

function Unblock-BackendArtifacts {
    param([string]$Root)

    $paths = @(
        (Join-Path $Root 'backend'),
        (Join-Path $Root 'dist')
    )

    foreach ($path in $paths) {
        if (-not (Test-Path $path)) {
            continue
        }
        try {
            if (Test-Path $path -PathType Container) {
                Get-ChildItem -Path $path -Recurse -File -ErrorAction SilentlyContinue | ForEach-Object {
                    Unblock-File -Path $_.FullName -ErrorAction SilentlyContinue
                }
            }
            else {
                Unblock-File -Path $path -ErrorAction SilentlyContinue
            }
        }
        catch {
            # Best-effort unblock only.
        }
    }
}

function Resolve-BackendCommand {
    param(
        [string]$Root,
        [int]$Port
    )

    $bundledExe = Join-Path $Root 'backend\PrintingBusinessApp.exe'
    if (Test-Path $bundledExe) {
        return @{
            filePath = $bundledExe
            args = @('--no-gui', '--port', "$Port", '--open-path', '/desktop-capture')
            mode = 'bundled-exe'
        }
    }

    $distExe = Join-Path $Root 'dist\PrintingBusinessApp.exe'
    if (Test-Path $distExe) {
        return @{
            filePath = $distExe
            args = @('--no-gui', '--port', "$Port", '--open-path', '/desktop-capture')
            mode = 'dist-exe'
        }
    }

    $pythonw = Join-Path $Root '.venv\Scripts\pythonw.exe'
    if ((Test-Path $pythonw) -and (Test-Path (Join-Path $Root 'app.py'))) {
        return @{
            filePath = $pythonw
            args = @('app.py', '--port', "$Port")
            mode = 'venv-pythonw'
        }
    }

    $python = Join-Path $Root '.venv\Scripts\python.exe'
    if ((Test-Path $python) -and (Test-Path (Join-Path $Root 'app.py'))) {
        return @{
            filePath = $python
            args = @('app.py', '--port', "$Port")
            mode = 'venv-python'
        }
    }

    return $null
}

$port = 5000

# Always replace whatever is listening on the Flask port so stale instances
# cannot keep serving old code.
$listeningPids = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue |
    Select-Object -ExpandProperty OwningProcess -Unique

foreach ($owningPid in ($listeningPids | Where-Object { $_ })) {
    Stop-Process -Id $owningPid -Force -ErrorAction SilentlyContinue
}

if ($listeningPids) {
    Start-Sleep -Milliseconds 500
}

$command = Resolve-BackendCommand -Root $projectRoot -Port $port
if (-not $command) {
    throw @"
No runnable backend was found.

Expected one of:
  1) backend\PrintingBusinessApp.exe (installer bundle runtime)
  2) dist\PrintingBusinessApp.exe (local built runtime)
  3) .venv\Scripts\pythonw.exe + app.py (source/dev runtime)

This usually means the extension package was installed without a backend runtime.
Rebuild the package using build_extension_packages.ps1 and redistribute that ZIP.
"@
}

Unblock-BackendArtifacts -Root $projectRoot
try {
    Start-Process -FilePath $command.filePath -ArgumentList $command.args -WorkingDirectory $projectRoot -WindowStyle Hidden -ErrorAction Stop
}
catch {
    $message = @"
Unable to start backend runtime.

Executable: $($command.filePath)
Mode: $($command.mode)

Windows may still be blocking this downloaded EXE.
Try one of these, then run Setup.bat again:
  1) Right-click backend\\PrintingBusinessApp.exe -> Properties -> check Unblock -> Apply
  2) In PowerShell: Unblock-File .\\backend\\PrintingBusinessApp.exe
  3) If antivirus blocked it, allow/restore the file and rerun setup

Original error: $($_.Exception.Message)
"@
    throw $message
}
Write-Host "Started Flask app in background on 127.0.0.1:$port (mode: $($command.mode))"
