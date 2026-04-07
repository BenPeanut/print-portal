$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $projectRoot

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

$pythonw = Join-Path $projectRoot '.venv\Scripts\pythonw.exe'
if (-not (Test-Path $pythonw)) {
    throw 'pythonw.exe was not found at .venv\Scripts\pythonw.exe'
}

Start-Process -FilePath $pythonw -ArgumentList @('app.py', '--port', "$port") -WorkingDirectory $projectRoot -WindowStyle Hidden
Write-Host "Started Flask app in background on 127.0.0.1:$port"
