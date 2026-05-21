$ErrorActionPreference = 'Stop'

Write-Host 'Extension packaging is no longer supported. The MakerWorld capture workflow is now integrated into the desktop app.' -ForegroundColor Yellow
Write-Host 'Run .\run_app.ps1 or python main.py to start the desktop app.' -ForegroundColor Yellow
return

$downloadsDir = Join-Path $projectRoot 'static\downloads'
if (-not (Test-Path $downloadsDir)) {
    New-Item -ItemType Directory -Path $downloadsDir -Force | Out-Null
}

$stageRoot = Join-Path $projectRoot 'var\extension_package_stage'
if (Test-Path $stageRoot) {
    Remove-Item $stageRoot -Recurse -Force
}
New-Item -ItemType Directory -Path $stageRoot -Force | Out-Null

function New-StageDir {
    param([string]$Name)
    $path = Join-Path $stageRoot $Name
    New-Item -ItemType Directory -Path $path -Force | Out-Null
    return $path
}

function Build-WindowsBundle {
    $windowsStage = New-StageDir -Name 'windows'

    $required = @(
        'makerworld_capture_extension',
        'Setup.bat',
        'bootstrap_extension_setup.ps1',
        'start_flask_background.ps1',
        'enable_flask_autostart.ps1',
        'disable_flask_autostart.ps1',
        'EXTENSION_SETUP.txt'
    )

    foreach ($item in $required) {
        $src = Join-Path $projectRoot $item
        if (-not (Test-Path $src)) {
            throw "Missing required file/folder for Windows bundle: $src"
        }
        Copy-Item -Path $src -Destination $windowsStage -Recurse -Force
    }

    $backendExe = Join-Path $projectRoot 'dist\PrintingBusinessApp.exe'
    if (-not (Test-Path $backendExe)) {
        throw @"
Missing backend runtime: dist\\PrintingBusinessApp.exe
Run .\\build_webview_exe.ps1 first, then rerun this packaging script.
"@
    }

    $backendDir = Join-Path $windowsStage 'backend'
    New-Item -ItemType Directory -Path $backendDir -Force | Out-Null
    Copy-Item -Path $backendExe -Destination (Join-Path $backendDir 'PrintingBusinessApp.exe') -Force

    $backendReadme = @'
Bundled backend runtime for MakerWorld extension.

This executable is started by start_flask_background.ps1 with:
  PrintingBusinessApp.exe --no-gui --port 5000 --open-path /desktop-capture

Do not remove this file. Without it, extension desktop mode will not start on machines without source code.
'@
    Set-Content -Path (Join-Path $backendDir 'README.txt') -Value $backendReadme -Encoding UTF8

    $zipPath = Join-Path $downloadsDir 'MakerWorld-Extension-Windows.zip'
    if (Test-Path $zipPath) {
        Remove-Item $zipPath -Force
    }

    Compress-Archive -Path (Join-Path $windowsStage '*') -DestinationPath $zipPath -CompressionLevel Optimal
    Write-Host "Built $zipPath" -ForegroundColor Green
}

function Build-MacBundle {
    $macStage = New-StageDir -Name 'macos'
    $required = @(
        'makerworld_capture_extension',
        'bootstrap_extension_setup.sh',
        'start_flask_background.sh',
        'disable_flask_autostart.sh',
        'EXTENSION_SETUP.txt'
    )

    foreach ($item in $required) {
        $src = Join-Path $projectRoot $item
        if (-not (Test-Path $src)) {
            throw "Missing required file/folder for macOS bundle: $src"
        }
        Copy-Item -Path $src -Destination $macStage -Recurse -Force
    }

    $zipPath = Join-Path $downloadsDir 'MakerWorld-Extension-macOS.zip'
    if (Test-Path $zipPath) {
        Remove-Item $zipPath -Force
    }

    Compress-Archive -Path (Join-Path $macStage '*') -DestinationPath $zipPath -CompressionLevel Optimal
    Write-Host "Built $zipPath" -ForegroundColor Green
}

function Build-ChromebookBundle {
    $chromeStage = New-StageDir -Name 'chromebook'
    $required = @(
        'makerworld_capture_extension',
        'CHROMEBOOK_EXTENSION_SETUP.txt'
    )

    foreach ($item in $required) {
        $src = Join-Path $projectRoot $item
        if (-not (Test-Path $src)) {
            throw "Missing required file/folder for Chromebook bundle: $src"
        }
        Copy-Item -Path $src -Destination $chromeStage -Recurse -Force
    }

    $zipPath = Join-Path $downloadsDir 'MakerWorld-Extension-Chromebook.zip'
    if (Test-Path $zipPath) {
        Remove-Item $zipPath -Force
    }

    Compress-Archive -Path (Join-Path $chromeStage '*') -DestinationPath $zipPath -CompressionLevel Optimal
    Write-Host "Built $zipPath" -ForegroundColor Green
}

Build-WindowsBundle
Build-MacBundle
Build-ChromebookBundle

# Sync var\extension_packages_build staging copies so they always match the ZIPs
$syncPairs = @(
    @{ Src = 'bootstrap_extension_setup.ps1'; Dst = 'var\extension_packages_build\windows\bootstrap_extension_setup.ps1' },
    @{ Src = 'start_flask_background.ps1';    Dst = 'var\extension_packages_build\windows\start_flask_background.ps1' },
    @{ Src = 'enable_flask_autostart.ps1';    Dst = 'var\extension_packages_build\windows\enable_flask_autostart.ps1' },
    @{ Src = 'disable_flask_autostart.ps1';   Dst = 'var\extension_packages_build\windows\disable_flask_autostart.ps1' },
    @{ Src = 'Setup.bat';                     Dst = 'var\extension_packages_build\windows\Setup.bat' },
    @{ Src = 'EXTENSION_SETUP.txt';           Dst = 'var\extension_packages_build\windows\EXTENSION_SETUP.txt' },
    @{ Src = 'bootstrap_extension_setup.sh';  Dst = 'var\extension_packages_build\macos\bootstrap_extension_setup.sh' },
    @{ Src = 'start_flask_background.sh';     Dst = 'var\extension_packages_build\macos\start_flask_background.sh' },
    @{ Src = 'disable_flask_autostart.sh';    Dst = 'var\extension_packages_build\macos\disable_flask_autostart.sh' },
    @{ Src = 'EXTENSION_SETUP.txt';           Dst = 'var\extension_packages_build\macos\EXTENSION_SETUP.txt' },
    @{ Src = 'CHROMEBOOK_EXTENSION_SETUP.txt'; Dst = 'var\extension_packages_build\chromebook\CHROMEBOOK_EXTENSION_SETUP.txt' }
)

foreach ($pair in $syncPairs) {
    $srcPath = Join-Path $projectRoot $pair.Src
    $dstPath = Join-Path $projectRoot $pair.Dst
    if (Test-Path $srcPath) {
        $dstDir = Split-Path -Parent $dstPath
        if (-not (Test-Path $dstDir)) {
            New-Item -ItemType Directory -Path $dstDir -Force | Out-Null
        }
        Copy-Item -Path $srcPath -Destination $dstPath -Force
    }
}

Write-Host ''
Write-Host 'Extension packages rebuilt in static\downloads' -ForegroundColor Green
Write-Host 'Staging copies in var\extension_packages_build synced' -ForegroundColor Green
