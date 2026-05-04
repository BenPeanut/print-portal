# Build Extension Packages
# Copies source extension to each platform build folder and applies platform-specific patches.
# Run from the project root: .\build_extension_packages.ps1
#
# Platforms produced:
#   var/extension_packages_build/windows/makerworld_capture_extension/
#   var/extension_packages_build/macos/makerworld_capture_extension/
#   var/extension_packages_build/chromebook/makerworld_capture_extension/
#
# Chromebook build: replaces localhost defaults with the hosted portal URL.
# Windows/macOS builds: keep localhost defaults (local Flask backend required).

$ErrorActionPreference = 'Stop'

$projectRoot  = Split-Path -Parent $MyInvocation.MyCommand.Path
$sourceExt    = Join-Path $projectRoot 'makerworld_capture_extension'
$buildBase    = Join-Path $projectRoot 'var\extension_packages_build'

$hostedPortal = 'https://print-portal-qm9p.onrender.com/'

$platforms = @('windows', 'macos', 'chromebook')

function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host ">> $Message" -ForegroundColor Cyan
}

function Copy-ExtensionSource {
    param([string]$DestDir)
    if (Test-Path $DestDir) {
        Remove-Item -Recurse -Force $DestDir
    }
    Copy-Item -Recurse -Force $sourceExt $DestDir
}

function Patch-ChromebookBackground {
    param([string]$BackgroundJs)

    $content = [System.IO.File]::ReadAllText($BackgroundJs, [System.Text.Encoding]::UTF8)

    # Normalize to LF so all string replacements work regardless of original line endings
    $content = $content.Replace("`r`n", "`n")

    # 1. HOSTED_PORTAL_BASE - ensure trailing slash
    $content = $content.Replace(
        'const HOSTED_PORTAL_BASE = "https://print-portal-qm9p.onrender.com";',
        'const HOSTED_PORTAL_BASE = "https://print-portal-qm9p.onrender.com/";')

    # 2. Add CHROMEBOOK_API_BASE after HOSTED_PORTAL_BASE (idempotent)
    if (-not $content.Contains('const CHROMEBOOK_API_BASE')) {
        $content = $content.Replace(
            'const HOSTED_PORTAL_BASE = "https://print-portal-qm9p.onrender.com/";',
            "const HOSTED_PORTAL_BASE = `"https://print-portal-qm9p.onrender.com/`";`nconst CHROMEBOOK_API_BASE = `"https://print-portal-qm9p.onrender.com/`";")
    }

    # 3. Fix HOSTED_EXTENSION_SETUP_URL double-slash
    $content = $content.Replace(
        'const HOSTED_EXTENSION_SETUP_URL = `${HOSTED_PORTAL_BASE}/extension-install`;',
        'const HOSTED_EXTENSION_SETUP_URL = `${HOSTED_PORTAL_BASE}extension-install`;')

    # 4. Replace localhost default in DEFAULT_SETTINGS
    $content = $content.Replace(
        'apiBase: "http://127.0.0.1:5000"',
        'apiBase: CHROMEBOOK_API_BASE')

    # 5. Replace remaining "http://127.0.0.1:5000" string literals
    $content = $content.Replace(
        '"http://127.0.0.1:5000"',
        'CHROMEBOOK_API_BASE')

    # 6. Point normalizeLocalApiBase fallback to CHROMEBOOK_API_BASE
    $content = $content.Replace(
        'const fallback = DEFAULT_SETTINGS.apiBase;',
        'const fallback = CHROMEBOOK_API_BASE;')

    # 7. Loosen the host restriction: replace the localhost-only guard with a
    #    protocol check so any valid https?:// URL is accepted
    $content = $content.Replace(
        '    const host = String(parsed.hostname || "").toLowerCase();' + "`n" +
        '    if (host !== "127.0.0.1" && host !== "localhost") {' + "`n" +
        '      return fallback;' + "`n" +
        '    }',
        '    if (!/^https?:$/i.test(parsed.protocol)) {' + "`n" +
        '      return fallback.replace(/\/$/, "");' + "`n" +
        '    }')

    # 8. Remove the debug force-true line in isChromeOS if it crept back in
    #    (source file should already have this removed; this is a safety pass)
    $debugLine  = '  return true; // TESTING:'
    if ($content.Contains($debugLine)) {
        $lines = $content -split "`n"
        $filtered = $lines | Where-Object { -not $_.TrimStart().StartsWith('return true; // TESTING:') -and
                                             -not $_.Contains('eslint-disable-line no-unreachable') }
        $content = $filtered -join "`n"
    }

    [System.IO.File]::WriteAllText($BackgroundJs, $content, [System.Text.Encoding]::UTF8)
}

# == Main ==

Write-Step "Building extension packages from source: $sourceExt"

foreach ($platform in $platforms) {
    Write-Step "[$platform] Copying source..."
    $destExt = Join-Path $buildBase "$platform\makerworld_capture_extension"
    Copy-ExtensionSource -DestDir $destExt
    Write-Host "  Copied to: $destExt"

    if ($platform -eq 'chromebook') {
        Write-Step "[chromebook] Patching background.js for hosted portal..."
        $bgJs = Join-Path $destExt 'background.js'
        Patch-ChromebookBackground -BackgroundJs $bgJs
        Write-Host "  API base locked to: $hostedPortal"

        # Verify no localhost strings remain
        $check = Get-Content -Raw $bgJs
        if ($check -match '127\.0\.0\.1') {
            Write-Host '  WARNING: 127.0.0.1 still found in patched file -- check regex' -ForegroundColor Yellow
        } else {
            Write-Host "  [OK] No localhost/127 references remain" -ForegroundColor Green
        }
    }
}

Write-Host ""
Write-Host "========================================" -ForegroundColor Green
Write-Host "Extension packages built successfully!"  -ForegroundColor Green
Write-Host "========================================"
Write-Host ""
Write-Host "Outputs:"
foreach ($platform in $platforms) {
    Write-Host "  $buildBase\$platform\makerworld_capture_extension"
}
Write-Host ""
Write-Host "Load the folder for the target platform as an unpacked Chrome extension."
