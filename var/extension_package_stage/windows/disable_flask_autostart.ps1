$ErrorActionPreference = 'Stop'

$startupFolder = [Environment]::GetFolderPath('Startup')
$shortcutPath = Join-Path $startupFolder 'PrintingBusiness Flask Background.lnk'

if (Test-Path $shortcutPath) {
    Remove-Item $shortcutPath -Force
    Write-Host "Autostart disabled: removed $shortcutPath" -ForegroundColor Yellow
} else {
    Write-Host "Autostart shortcut not found. Nothing to remove."
}
