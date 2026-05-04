# Bootstrap setup script for Printing Business MakerWorld extension
# This script must be run once after installing the extension to:
#   1. Collect and store connection secrets as user-level environment variables
#   2. Register Flask backend for automatic startup on user login
#   3. Launch the backend immediately for testing
#
# Run as: powershell.exe -ExecutionPolicy Bypass -File bootstrap_extension_setup.ps1

$ErrorActionPreference = 'Stop'

# Logging setup
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$logDir = Join-Path $projectRoot 'var'
if (-not (Test-Path $logDir)) {
    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
}
$logPath = Join-Path $logDir "bootstrap-$(Get-Date -Format 'yyyyMMdd_HHmmss').log"

function Write-Log {
    param([string]$Message, [string]$Level = 'INFO')
    $timestamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    $logEntry = "[$timestamp] [$Level] $Message"
    Write-Host $logEntry
    Add-Content -Path $logPath -Value $logEntry -ErrorAction SilentlyContinue
}

Write-Log "=========================================="
Write-Log "MakerWorld Extension Bootstrap Setup"
Write-Log "=========================================="
Write-Log "Log file: $logPath"
Write-Log "Project root: $projectRoot"

# Step 1: Collect environment variables from user
Write-Log "Step 1: Collecting database connection credentials"
Write-Host ""
Write-Host "You need to provide your database connection string."
Write-Host "This will be saved as a user-level environment variable (not shared across machines)."
Write-Host "Example: postgresql://user:password@host:5432/dbname"
Write-Host ""

$dbUrl = Read-Host "Enter DATABASE_URL (you can paste from clipboard)"
if (-not $dbUrl) {
    Write-Log "ERROR: DATABASE_URL cannot be empty" "ERROR"
    throw "DATABASE_URL is required"
}
Write-Log "DATABASE_URL collected (length: $($dbUrl.Length) chars)"

$apiKey = Read-Host "Optional: Enter EXTENSION_API_KEY (press Enter to skip)"
if ($apiKey) {
    Write-Log "EXTENSION_API_KEY collected (length: $($apiKey.Length) chars)"
}
else {
    Write-Log "EXTENSION_API_KEY not provided. Session-based localhost auth will be used."
}

# Step 2: Set environment variables at user level
Write-Log "Step 2: Registering environment variables at user level"
try {
    [Environment]::SetEnvironmentVariable('DATABASE_URL', $dbUrl, 'User')
    Write-Log "[OK] DATABASE_URL registered"
    
    if ($apiKey) {
        [Environment]::SetEnvironmentVariable('EXTENSION_API_KEY', $apiKey, 'User')
        Write-Log "[OK] EXTENSION_API_KEY registered"
    }
    else {
        [Environment]::SetEnvironmentVariable('EXTENSION_API_KEY', $null, 'User')
        Write-Log "[OK] EXTENSION_API_KEY cleared from user environment"
    }
    
    # Make variables available to current process
    $env:DATABASE_URL = $dbUrl
    if ($apiKey) {
        $env:EXTENSION_API_KEY = $apiKey
    }
    else {
        Remove-Item Env:EXTENSION_API_KEY -ErrorAction SilentlyContinue
    }
    Write-Log "[OK] Environment variables available in current session"
}
catch {
    Write-Log "ERROR: Failed to set environment variables: $_" "ERROR"
    throw $_
}

# Step 3: Register autostart
Write-Log "Step 3: Registering Flask backend for autostart at login"
try {
    $startupFolder = [Environment]::GetFolderPath('Startup')
    $shortcutPath = Join-Path $startupFolder 'PrintingBusiness Flask Background.lnk'
    $starterScript = Join-Path $projectRoot 'start_flask_background.ps1'
    
    if (-not (Test-Path $starterScript)) {
        Write-Log "ERROR: Starter script not found: $starterScript" "ERROR"
        throw "Missing start_flask_background.ps1"
    }
    
    # Create shortcut using WScript.Shell COM object
    $wsh = New-Object -ComObject WScript.Shell
    $shortcut = $wsh.CreateShortcut($shortcutPath)
    $shortcut.TargetPath = 'powershell.exe'
    $shortcut.Arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$starterScript`""
    $shortcut.WorkingDirectory = $projectRoot
    $shortcut.IconLocation = "$env:SystemRoot\System32\shell32.dll,167"
    $shortcut.Description = 'Starts Printing Business Flask server in the background at sign-in.'
    $shortcut.Save()
    
    Write-Log "[OK] Startup shortcut created: $shortcutPath"
}
catch {
    Write-Log "ERROR: Failed to register autostart: $_" "ERROR"
    throw $_
}

# Step 4: Launch backend immediately for verification
Write-Log "Step 4: Launching Flask backend for immediate testing"
try {
    $pythonw = Join-Path $projectRoot '.venv\Scripts\pythonw.exe'
    if (-not (Test-Path $pythonw)) {
        Write-Log "WARNING: pythonw.exe not found, will try python.exe" "WARN"
        $pythonw = Join-Path $projectRoot '.venv\Scripts\python.exe'
    }
    
    if (-not (Test-Path $pythonw)) {
        Write-Log "ERROR: Python executable not found at $pythonw" "ERROR"
        throw "Python not found"
    }
    
    # Kill any existing Flask processes on port 5000
    $listeningPids = Get-NetTCPConnection -LocalPort 5000 -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique
    foreach ($owningPid in ($listeningPids | Where-Object { $_ })) {
        Stop-Process -Id $owningPid -Force -ErrorAction SilentlyContinue
        Write-Log "Stopped existing process (PID: $owningPid) listening on port 5000"
    }
    
    Start-Sleep -Milliseconds 500
    
    # Start Flask backend
    Start-Process -FilePath $pythonw -ArgumentList @('app.py', '--port', '5000') -WorkingDirectory $projectRoot -WindowStyle Hidden
    
    Write-Log "[OK] Flask backend process launched (port 5000)"
    
    # Wait and test connectivity
    Write-Log "Waiting for backend to become available..."
    $maxRetries = 15
    $retryCount = 0
    $healthCheckPassed = $false
    
    while ($retryCount -lt $maxRetries) {
        Start-Sleep -Seconds 1
        $retryCount++
        
        try {
            $response = Invoke-WebRequest -Uri 'http://127.0.0.1:5000/api/health' -TimeoutSec 2 -ErrorAction SilentlyContinue
            if ($response.StatusCode -eq 200) {
                Write-Log "[OK] Backend health check passed (attempt $retryCount)"
                $healthCheckPassed = $true
                break
            }
        }
        catch {
            # Still starting, continue retrying
        }
    }
    
    if (-not $healthCheckPassed) {
        Write-Log "WARNING: Backend did not respond within timeout" "WARN"
    }
}
catch {
    Write-Log "ERROR: Failed to launch Flask backend: $_" "ERROR"
    throw $_
}

# Step 5: Summary and next steps
Write-Log ""
Write-Log "=========================================="
Write-Log "[OK] Bootstrap setup completed successfully!"
Write-Log "=========================================="
Write-Log ""
Write-Log "NEXT STEPS:"
Write-Log "  1. Open the MakerWorld extension popup in Chrome"
Write-Log "  2. You should see the login form"
Write-Log "  3. Log in with your username and password"
Write-Log "  4. Navigate to a MakerWorld model page"
Write-Log "  5. Hover over a model and press Q to open the order popup"
Write-Log ""
Write-Log "AUTOSTART VERIFICATION:"
Write-Log "  The Flask backend will now start automatically when you log into Windows."
Write-Log "  To verify: restart your computer and check Task Manager for python.exe process."
Write-Log ""
Write-Log "TROUBLESHOOTING:"
Write-Log "  - To disable autostart: Delete 'PrintingBusiness Flask Background.lnk' from Startup folder"
Write-Log "  - To update secrets: Run this script again"
Write-Log "  - To manually start backend: Run start_flask_background.ps1"
Write-Log "  - Check log file: $logPath"
Write-Log ""
Write-Log "=========================================="

Write-Host ""
Write-Host "Setup complete! Press any key to exit..."
$null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
