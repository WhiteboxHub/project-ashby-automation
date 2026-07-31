# ----------------------------------------------------------------------
#  WboxCLI Global Installer - Windows (PowerShell)
#
#  Usage:
#    irm https://raw.githubusercontent.com/WhiteboxHub/project-ashby-automation/main/scripts/install.ps1 | iex
#
#  What it does:
#    1. Clones (or updates) the repo into %USERPROFILE%\.jobcli\src
#    2. Creates a Python venv at %USERPROFILE%\.jobcli\venv
#    3. Installs the package + Playwright Chromium
#    4. Drops wboxcli.cmd at %USERPROFILE%\.local\bin\
#    5. Adds %USERPROFILE%\.local\bin to the user PATH (if not already there)
#    6. Launches the interactive TUI
# ----------------------------------------------------------------------

$ErrorActionPreference = "Stop"

# -- Config -----------------------------------------------------------
$InstallDir    = Join-Path $env:USERPROFILE ".jobcli"
$SrcDir        = Join-Path $InstallDir "src"
$VenvDir       = Join-Path $InstallDir "venv"
$BinDir        = Join-Path $env:USERPROFILE ".local\bin"
$Wrapper       = Join-Path $BinDir "wboxcli.cmd"
$RepoUrl       = "https://github.com/WhiteboxHub/project-ashby-automation.git"
$Branch        = if ($env:JOBCLI_BRANCH) { $env:JOBCLI_BRANCH } else { "main" }

function Write-Step   { param($msg) Write-Host "[info]  $msg" -ForegroundColor Cyan }
function Write-Ok     { param($msg) Write-Host "[OK]    $msg" -ForegroundColor Green }
function Write-Warn   { param($msg) Write-Host "[WARN]  $msg" -ForegroundColor Yellow }
function Write-Fail   { param($msg) Write-Host "[FAIL]  $msg" -ForegroundColor Red; exit 1 }

Write-Host ""
Write-Host "****************************************" -ForegroundColor Cyan
Write-Host "*     WboxCLI - Global Installer       *" -ForegroundColor Cyan
Write-Host "****************************************" -ForegroundColor Cyan
Write-Host ""

# -- Step 1: Prerequisites --------------------------------------------
Write-Step "Checking prerequisites..."

# Python 3.10+
try {
    $pyVersion = & python --version 2>&1
    if ($pyVersion -match "Python (\d+)\.(\d+)") {
        $pyMajor = [int]$Matches[1]
        $pyMinor = [int]$Matches[2]
        if ($pyMajor -lt 3 -or ($pyMajor -eq 3 -and $pyMinor -lt 10)) {
            Write-Fail "Python 3.10+ is required (found $pyVersion)."
        }
        Write-Ok "Python $($Matches[1]).$($Matches[2])"
    } else {
        Write-Fail "Could not determine Python version."
    }
} catch {
    Write-Fail "python is not installed. Install Python 3.10+ from https://python.org"
}

# Git
try {
    $null = & git --version 2>&1
    Write-Ok "git found"
} catch {
    Write-Fail "git is not installed. Install from https://git-scm.com"
}

# -- Step 2: Clone or update repo -------------------------------------
Write-Step "Setting up source at $SrcDir..."

$dbPath = Join-Path $InstallDir "jobcli.db"
if ($env:JOBCLI_FRESH_INSTALL -eq "1") {
    Write-Step "JOBCLI_FRESH_INSTALL=1 — resetting local database..."
    Remove-Item -Path (Join-Path $InstallDir "jobcli.db*") -Force -ErrorAction SilentlyContinue
    Write-Ok "Removed existing settings"
} elseif (Test-Path $dbPath) {
    Write-Ok "Keeping existing login and settings (jobcli.db)"
}

if (-not (Test-Path $InstallDir)) {
    New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
}

$ExtTmpDir = Join-Path $env:TEMP "jobcli_ext_clone_$PID"
$ExtUrl = "https://github.com/WhiteboxHub/project-talentscreen-autofill-extension.git"

Write-Step "Cloning TalentScreen extension..."
& git clone --depth 1 $ExtUrl $ExtTmpDir --quiet 2>$null
Write-Ok "Cloned extension to temporary location"

if (Test-Path (Join-Path $SrcDir ".git\HEAD")) {
    Write-Step "Existing installation found - pulling latest..."
    & git -C $SrcDir remote set-branches origin '*' 2>$null
    & git -C $SrcDir fetch origin --depth 1 $Branch --quiet 2>$null
    & git -C $SrcDir checkout -B $Branch "origin/$Branch" --quiet 2>$null
    Write-Ok "Updated to latest $Branch"
} else {
    if (Test-Path $SrcDir) {
        Remove-Item -Recurse -Force $SrcDir -ErrorAction SilentlyContinue
    }
    & git clone --branch $Branch --depth 1 $RepoUrl $SrcDir --quiet 2>$null
    Write-Ok "Cloned $Branch branch"
}

$BinDirTarget = Join-Path $SrcDir "bin"
if (-not (Test-Path $BinDirTarget)) {
    New-Item -ItemType Directory -Path $BinDirTarget -Force | Out-Null
}
$ExtTarget = Join-Path $BinDirTarget "project-talentscreen-autofill-extension"
if (Test-Path $ExtTarget) {
    Remove-Item -Recurse -Force $ExtTarget
}
Move-Item -Path $ExtTmpDir -Destination $ExtTarget -Force
Write-Ok "Attached extension to bin directory"
# -- Step 3: Create venv and install ----------------------------------
Write-Step "Setting up Python virtual environment..."

$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
$VenvPip    = Join-Path $VenvDir "Scripts\pip.exe"

if (-not (Test-Path $VenvPython)) {
    & python -m venv $VenvDir
    Write-Ok "Created venv at $VenvDir"
} else {
    Write-Ok "Venv already exists"
}

Write-Step "Installing wboxcli and dependencies (this may take a minute)..."

# Upgrade pip using python -m to avoid "binary in use" errors on Windows
& $VenvPython -m pip install --upgrade pip --quiet 2>$null
& $VenvPython -m pip install -e $SrcDir --quiet 2>$null

if ($LASTEXITCODE -ne 0) {
    Write-Warn "pip install had issues - retrying with verbose output..."
    & $VenvPython -m pip install -e $SrcDir
}

Write-Ok "wboxcli installed"

# -- Step 4: Install Playwright browsers ------------------------------
Write-Step "Installing Playwright Chromium browser..."

try {
    & $VenvPython -m playwright install chromium 2>$null
    Write-Ok "Playwright Chromium ready"
} catch {
    Write-Warn "Playwright browser install had issues - run 'wboxcli doctor' to verify."
}

# -- Step 5: Create global wrapper scripts -----------------------------
Write-Step "Creating global commands at $BinDir..."

if (-not (Test-Path $BinDir)) {
    New-Item -ItemType Directory -Path $BinDir -Force | Out-Null
}

# Primary command: wboxcli (always uses venv python -m jobcli.cli.entry)
$WboxcliContent = @"
@echo off
set "JOBCLI_VENV=%USERPROFILE%\.jobcli\venv"
set "JOBCLI_PY=%JOBCLI_VENV%\Scripts\python.exe"
if not exist "%JOBCLI_PY%" (
    echo Error: WboxCLI installation not found at %JOBCLI_VENV%
    echo Re-install with the provided command.
    exit /b 1
)
"%JOBCLI_PY%" -m jobcli.cli.entry %*
"@
Set-Content -Path $Wrapper -Value $WboxcliContent -Encoding ASCII

Write-Ok "Command created: wboxcli"

# Remove broken global pip shims that shadow the managed install
Write-Step "Removing stale global wboxcli shims (if any)..."
try {
    $removed = & $VenvPython -c "from jobcli.cli.launcher import remove_stale_global_shims; print(chr(10).join(remove_stale_global_shims()))" 2>$null
    if ($removed) {
        $removed -split "`n" | Where-Object { $_ } | ForEach-Object { Write-Ok "Removed $_" }
    } else {
        Write-Ok "No conflicting shims found"
    }
} catch {
    Write-Warn "Could not scan for stale shims (safe to ignore on first install)"
}

# -- Step 6: Ensure ~/.local/bin is FIRST on user PATH ----------------
Write-Step "Checking PATH..."

$CurrentUserPath = [Environment]::GetEnvironmentVariable("Path", "User")
$PathParts = @($CurrentUserPath -split ';' | Where-Object { $_ -and ($_ -ne $BinDir) })
$NewPath = "$BinDir;" + ($PathParts -join ';')
$NeedsRestart = $false
if ($CurrentUserPath -notlike "*$BinDir*") {
    Write-Step "Adding $BinDir to user PATH (first)..."
    [Environment]::SetEnvironmentVariable("Path", $NewPath, "User")
    $env:Path = "$BinDir;$env:Path"
    Write-Ok "Added to user PATH"
    $NeedsRestart = $true
} elseif (-not $CurrentUserPath.StartsWith("$BinDir;") -and $CurrentUserPath -ne $BinDir) {
    Write-Step "Moving $BinDir to front of user PATH..."
    [Environment]::SetEnvironmentVariable("Path", $NewPath, "User")
    $env:Path = "$BinDir;$env:Path"
    Write-Ok "PATH updated"
    $NeedsRestart = $true
} else {
    Write-Ok "$BinDir already first on PATH"
}

# -- Step 7: (no .env — config is saved interactively via `wboxcli login`) ---

# -- Done! -------------------------------------------------------------
Write-Host ""
Write-Host "****************************************" -ForegroundColor Green
Write-Host "*       Installation Complete!         *" -ForegroundColor Green
Write-Host "****************************************" -ForegroundColor Green
Write-Host ""
Write-Host "  Install dir  : $InstallDir" -ForegroundColor Cyan
Write-Host "  Command      : wboxcli" -ForegroundColor Cyan
Write-Host ""

if ($NeedsRestart) {
    Write-Host "  [!] Restart your terminal for PATH changes to take effect." -ForegroundColor Yellow
    Write-Host ""
}

Write-Host "  Daily command (until uninstall):  wboxcli" -ForegroundColor Green
Write-Host "  Login is saved in ~/.jobcli/jobcli.db until reset or uninstall." -ForegroundColor DarkGray
Write-Host ""

# -- Auto-launch interactive TUI --------------------------------------
Write-Host "  Launching WboxCLI..." -ForegroundColor White
Write-Host ""

if (Test-Path $VenvPython) {
    & $VenvPython -m jobcli.cli.entry
} else {
    Write-Host "  Next steps:" -ForegroundColor White
    Write-Host "    1. Restart your terminal" -ForegroundColor Cyan
    Write-Host "    2. Type: wboxcli" -ForegroundColor Cyan
    Write-Host ""
}
