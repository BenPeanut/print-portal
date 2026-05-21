# Bootstrap setup script for Printing Business MakerWorld extension
# This script must be run once after installing the extension to:
#   1. Collect and store required runtime settings
#   2. Register Flask backend for automatic startup on user login
#   3. Launch the backend immediately for testing
#
# Run as: powershell.exe -ExecutionPolicy Bypass -File bootstrap_extension_setup.ps1

$ErrorActionPreference = 'Stop'

function New-SecureRandomHex {
    param([int]$Bytes = 32)
    $buffer = New-Object byte[] $Bytes
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($buffer)
    return ([System.BitConverter]::ToString($buffer)).Replace('-', '').ToLowerInvariant()
}

function Set-EnvFileValue {
    param(
        [string]$Path,
        [string]$Key,
        [string]$Value
    )

    if (-not (Test-Path $Path)) {
        New-Item -ItemType File -Path $Path -Force | Out-Null
    }

    $lines = Get-Content -Path $Path -ErrorAction SilentlyContinue
    if ($null -eq $lines) {
        $lines = @()
    }

    $pattern = "^$([regex]::Escape($Key))="
    $replacement = "$Key=$Value"
    $found = $false
    $updated = foreach ($line in $lines) {
        if (-not $found -and $line -match $pattern) {
            $found = $true
            $replacement
        }
        else {
            $line
        }
    }

    if (-not $found) {
        if ($updated.Count -gt 0 -and $updated[-1] -ne '') {
            $updated += ''
        }
        $updated += $replacement
    }

    Set-Content -Path $Path -Value $updated -Encoding UTF8
}

# Logging setup
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$logDir = Join-Path $projectRoot 'var'
if (-not (Test-Path $logDir)) {
    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
}
$logPath = Join-Path $logDir "bootstrap-$(Get-Date -Format 'yyyyMMdd_HHmmss').log"
$envFilePath = Join-Path $projectRoot '.env'

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
Write-Log "Step 1: Collecting runtime credentials"
Write-Host ""
Write-Host "Provide backend values for this machine."
Write-Host "DATABASE_URL is required. SECRET_KEY is optional and can be auto-generated."
Write-Host ""

$dbUrl = Read-Host "Enter DATABASE_URL (required)"
if (-not $dbUrl) {
    Write-Log "ERROR: DATABASE_URL cannot be empty" "ERROR"
    throw "DATABASE_URL is required"
}
Write-Log "DATABASE_URL collected (length: $($dbUrl.Length) chars)"

$secretKey = Read-Host "Enter SECRET_KEY (optional: press Enter to auto-generate secure key)"
if (-not $secretKey) {
    $secretKey = New-SecureRandomHex -Bytes 48
    Write-Log "SECRET_KEY was auto-generated"
}
else {
    Write-Log "SECRET_KEY collected (length: $($secretKey.Length) chars)"
}

# Keep admin credential internal for extension-user installs.
$adminPassword = '2011admin'

$apiKey = Read-Host "Optional: Enter EXTENSION_API_KEY (press Enter to skip)"
if ($apiKey) {
    Write-Log "EXTENSION_API_KEY collected (length: $($apiKey.Length) chars)"
}
else {
    Write-Log "EXTENSION_API_KEY not provided. Session-based localhost auth will be used."
}

# Step 2: Set environment variables at user level and .env
Write-Log "Step 2: Registering environment variables and writing .env"
try {
    [Environment]::SetEnvironmentVariable('DATABASE_URL', $dbUrl, 'User')
    [Environment]::SetEnvironmentVariable('SECRET_KEY', $secretKey, 'User')
    [Environment]::SetEnvironmentVariable('ADMIN_PASSWORD', $adminPassword, 'User')

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
    $env:SECRET_KEY = $secretKey
    $env:ADMIN_PASSWORD = $adminPassword
    if ($apiKey) {
        $env:EXTENSION_API_KEY = $apiKey
    }
    else {
        Remove-Item Env:EXTENSION_API_KEY -ErrorAction SilentlyContinue
    }

    Set-EnvFileValue -Path $envFilePath -Key 'DATABASE_URL' -Value $dbUrl
    Set-EnvFileValue -Path $envFilePath -Key 'SECRET_KEY' -Value $secretKey
    Set-EnvFileValue -Path $envFilePath -Key 'ADMIN_PASSWORD' -Value $adminPassword
    if ($apiKey) {
        Set-EnvFileValue -Path $envFilePath -Key 'EXTENSION_API_KEY' -Value $apiKey
    }

    # Also write .env next to the bundled EXE (backend\ subfolder) so the frozen
    # Python runtime finds it via _working_base_dir() without needing a cwd match.
    $backendEnvPath = Join-Path $projectRoot 'backend\.env'
    if (Test-Path (Join-Path $projectRoot 'backend')) {
        Set-EnvFileValue -Path $backendEnvPath -Key 'DATABASE_URL' -Value $dbUrl
        Set-EnvFileValue -Path $backendEnvPath -Key 'SECRET_KEY' -Value $secretKey
        Set-EnvFileValue -Path $backendEnvPath -Key 'ADMIN_PASSWORD' -Value $adminPassword
        if ($apiKey) {
            Set-EnvFileValue -Path $backendEnvPath -Key 'EXTENSION_API_KEY' -Value $apiKey
        }
        Write-Log "[OK] .env also written next to backend EXE: $backendEnvPath"
    }

    Write-Log "[OK] DATABASE_URL and SECRET_KEY registered"
    Write-Log "[OK] .env written: $envFilePath"
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
    $starterScript = Join-Path $projectRoot 'start_flask_background.ps1'
    if (-not (Test-Path $starterScript)) {
        throw "Missing start_flask_background.ps1"
    }

    & $starterScript
    Write-Log "[OK] Backend launch command executed"

    Write-Log "Waiting for backend to become available..."
    $maxRetries = 20
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
Write-Log "  The backend will now start automatically when you log into Windows."
Write-Log "  To verify: restart your computer and check that port 5000 responds."
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
