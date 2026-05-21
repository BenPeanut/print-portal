$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$startupFolder = [Environment]::GetFolderPath('Startup')
$shortcutPath = Join-Path $startupFolder 'PrintingBusiness Flask Background.lnk'
$starterScript = Join-Path $projectRoot 'start_flask_background.ps1'

if (-not (Test-Path $starterScript)) {
    throw "Missing script: $starterScript"
}

$wsh = New-Object -ComObject WScript.Shell
$shortcut = $wsh.CreateShortcut($shortcutPath)
$shortcut.TargetPath = 'powershell.exe'
$shortcut.Arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$starterScript`""
$shortcut.WorkingDirectory = $projectRoot
$shortcut.IconLocation = "$env:SystemRoot\System32\shell32.dll,167"
$shortcut.Description = 'Starts Printing Business Flask server in the background at sign-in.'
$shortcut.Save()

Write-Host "Autostart enabled: $shortcutPath" -ForegroundColor Green
