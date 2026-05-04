$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $projectRoot

function Get-PythonLaunchCommand {
    param([string]$ProjectRoot)

    $venvPythonw = Join-Path $ProjectRoot '.venv\Scripts\pythonw.exe'
    if (Test-Path $venvPythonw) {
        return @{ Path = $venvPythonw; Args = @() }
    }

    $venvPython = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
    if (Test-Path $venvPython) {
        return @{ Path = $venvPython; Args = @() }
    }

    $pyLauncher = Get-Command py -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($pyLauncher -and $pyLauncher.Source) {
        return @{ Path = $pyLauncher.Source; Args = @('-3') }
    }

    $pythonwCmd = Get-Command pythonw -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($pythonwCmd -and $pythonwCmd.Source) {
        return @{ Path = $pythonwCmd.Source; Args = @() }
    }

    $pythonCmd = Get-Command python -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($pythonCmd -and $pythonCmd.Source) {
        return @{ Path = $pythonCmd.Source; Args = @() }
    }

    return $null
}

function Test-PortOpen {
    param([int]$Port)
    try {
        $client = New-Object System.Net.Sockets.TcpClient
        $iar = $client.BeginConnect('127.0.0.1', $Port, $null, $null)
        $ok = $iar.AsyncWaitHandle.WaitOne(400)
        if ($ok -and $client.Connected) {
            $client.EndConnect($iar) | Out-Null
            $client.Close()
            return $true
        }
        $client.Close()
        return $false
    } catch {
        return $false
    }
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

$pythonLaunch = Get-PythonLaunchCommand -ProjectRoot $projectRoot
if (-not $pythonLaunch) {
    throw 'Python executable not found. Checked .venv, py launcher, and PATH.'
}

$launchArgs = @()
if ($pythonLaunch.Args) {
    $launchArgs += $pythonLaunch.Args
}
$launchArgs += @('app.py', '--port', "$port")

Start-Process -FilePath $pythonLaunch.Path -ArgumentList $launchArgs -WorkingDirectory $projectRoot -WindowStyle Hidden
Write-Host "Started Flask app in background on 127.0.0.1:$port"
