# ──────────────────────────────────────────────────────────────────────
#  WboxCLI Global Uninstaller — Windows (PowerShell)
#
#  Usage:
#    irm https://raw.githubusercontent.com/WhiteboxHub/project-ashby-automation/main/scripts/uninstall.ps1 | iex
# ──────────────────────────────────────────────────────────────────────

$InstallDir = Join-Path $env:USERPROFILE ".jobcli"
$BinDir     = Join-Path $env:USERPROFILE ".local\bin"
$Wrappers   = @(
    (Join-Path $BinDir "wboxcli.cmd")
)

Write-Host ""
Write-Host "WboxCLI - Uninstaller" -ForegroundColor Red
Write-Host ""

$confirm = Read-Host "This will delete $InstallDir and the wboxcli shim in $BinDir. Continue? [y/N]"
if ($confirm -notmatch "^[Yy]$") {
    Write-Host "Cancelled." -ForegroundColor Yellow
    exit 0
}

# Remove wrappers
foreach ($w in $Wrappers) {
    if (Test-Path $w) {
        try {
            Remove-Item -Force $w
            Write-Host "[OK]    Removed $w" -ForegroundColor Green
        } catch {
            Write-Host "[WARN]  Could not remove $w : $_" -ForegroundColor Yellow
        }
    } else {
        Write-Host "[--]    Not found: $w" -ForegroundColor Yellow
    }
}

# Remove install dir (retry once after a short pause in case Python locked the venv)
if (Test-Path $InstallDir) {
    try {
        Remove-Item -Recurse -Force $InstallDir -ErrorAction Stop
        Write-Host "[OK]    Removed $InstallDir" -ForegroundColor Green
    } catch {
        Start-Sleep -Seconds 1
        try {
            Remove-Item -Recurse -Force $InstallDir -ErrorAction Stop
            Write-Host "[OK]    Removed $InstallDir" -ForegroundColor Green
        } catch {
            Write-Host "[FAIL]  Could not delete $InstallDir : $_" -ForegroundColor Red
            Write-Host "        Close any open terminals running wboxcli and re-run this script." -ForegroundColor Yellow
        }
    }
} else {
    Write-Host "[--]    Install directory not found at $InstallDir" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "WboxCLI has been uninstalled." -ForegroundColor Green
Write-Host "Note: The PATH entry was left intact - remove $BinDir manually from System Environment Variables if you wish." -ForegroundColor Yellow
Write-Host ""
