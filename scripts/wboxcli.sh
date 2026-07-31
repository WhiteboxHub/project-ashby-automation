#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────
#  WboxCLI — Management Script
#
#  Usage:
#    ./scripts/wboxcli.sh <command>
#
#  Commands:
#    install     Install WboxCLI globally (same as the one-liner installer)
#    setup       Re-run the initial onboarding wizard (change credentials/LLM/resume)
#    update      Pull the latest code and reinstall dependencies
#    uninstall   Remove WboxCLI completely (config, venv, shims)
#    reset       Clear login, API keys, resume (jobs kept) and re-run setup
#    clear-jobs  Delete discovered jobs only (keeps credentials/resume)
#    doctor      Run environment health checks
#    status      Show current configuration status
#    help        Show this help message
# ──────────────────────────────────────────────────────────────────────

set -euo pipefail

# ── Colors ───────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
DIM='\033[2m'
NC='\033[0m' # No Color

info()  { echo -e "${CYAN}[info]${NC}  $*"; }
ok()    { echo -e "${GREEN}[✓]${NC}    $*"; }
warn()  { echo -e "${YELLOW}[warn]${NC}  $*"; }
fail()  { echo -e "${RED}[✗]${NC}    $*"; exit 1; }
header() {
    echo ""
    echo -e "${BOLD}${CYAN}╔══════════════════════════════════════╗${NC}"
    echo -e "${BOLD}${CYAN}║     WboxCLI — $1${NC}"
    echo -e "${BOLD}${CYAN}╚══════════════════════════════════════╝${NC}"
    echo ""
}

# ── Config ───────────────────────────────────────────────────────────
INSTALL_DIR="$HOME/.jobcli"
SRC_DIR="$INSTALL_DIR/src"
VENV_DIR="$INSTALL_DIR/venv"
BIN_DIR="$HOME/.local/bin"
WRAPPER="$BIN_DIR/wboxcli"
REPO_URL="https://github.com/WhiteboxHub/project-ashby-automation.git"
BRANCH="${JOBCLI_BRANCH:-main}"
DB_FILE="$INSTALL_DIR/jobcli.db"

# ── Helper: find the wboxcli Python ──────────────────────────────────
_venv_python() {
    local py="$VENV_DIR/bin/python"
    if [ -f "$py" ]; then
        echo "$py"
    else
        echo "python3"
    fi
}

_ensure_installed() {
    if [ ! -d "$INSTALL_DIR" ] || [ ! -f "$VENV_DIR/bin/python" ]; then
        fail "WboxCLI is not installed. Run: $0 install"
    fi
}

# ══════════════════════════════════════════════════════════════════════
#  INSTALL
# ══════════════════════════════════════════════════════════════════════
cmd_install() {
    header "Global Installer       ║"

    # Prerequisites
    info "Checking prerequisites..."

    if ! command -v python3 &>/dev/null; then
        fail "python3 is not installed. Install Python 3.10+ first."
    fi

    PY_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
    PY_MAJOR=$(echo "$PY_VERSION" | cut -d. -f1)
    PY_MINOR=$(echo "$PY_VERSION" | cut -d. -f2)

    if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 10 ]; }; then
        fail "Python 3.10+ is required (found $PY_VERSION)."
    fi
    ok "Python $PY_VERSION"

    if ! command -v git &>/dev/null; then
        fail "git is not installed."
    fi
    ok "git found"

    # Clone or update repo
    info "Setting up source at $SRC_DIR..."

    if [ -d "$INSTALL_DIR" ]; then
        info "Cleaning up existing database and settings..."
        rm -f "$INSTALL_DIR"/jobcli.db*
        ok "Removed existing settings"
    fi

    mkdir -p "$INSTALL_DIR"

    if [ -d "$SRC_DIR/.git" ]; then
        info "Existing installation found — pulling latest..."
        git -C "$SRC_DIR" fetch origin "$BRANCH" --depth 1 --quiet
        git -C "$SRC_DIR" checkout -B "$BRANCH" FETCH_HEAD --quiet
        ok "Updated to latest $BRANCH"
    else
        git clone --branch "$BRANCH" --depth 1 "$REPO_URL" "$SRC_DIR" --quiet
        ok "Cloned $BRANCH branch"
    fi

    # Clone TalentScreen extension
    EXT_TMP_DIR="/tmp/wboxcli_ext_clone_$$"
    EXT_URL="https://github.com/WhiteboxHub/project-talentscreen-autofill-extension.git"

    info "Cloning TalentScreen extension..."
    git clone --depth 1 "$EXT_URL" "$EXT_TMP_DIR" --quiet
    mkdir -p "$SRC_DIR/bin"
    rm -rf "$SRC_DIR/bin/project-talentscreen-autofill-extension"
    mv "$EXT_TMP_DIR" "$SRC_DIR/bin/project-talentscreen-autofill-extension"
    ok "Attached extension to bin directory"

    # Create venv and install
    info "Setting up Python virtual environment..."
    if [ ! -d "$VENV_DIR" ]; then
        python3 -m venv "$VENV_DIR"
        ok "Created venv at $VENV_DIR"
    else
        ok "Venv already exists"
    fi

    info "Installing wboxcli and dependencies (this may take a minute)..."
    "$VENV_DIR/bin/pip" install --upgrade pip --quiet 2>/dev/null
    "$VENV_DIR/bin/pip" install -e "$SRC_DIR" --quiet 2>/dev/null
    ok "wboxcli installed"

    # Install Playwright
    info "Installing Playwright Chromium browser..."
    "$VENV_DIR/bin/python" -m playwright install chromium --quiet 2>/dev/null || \
        "$VENV_DIR/bin/python" -m playwright install chromium 2>/dev/null || \
        warn "Playwright browser install had issues — run 'wboxcli doctor' to verify."
    ok "Playwright Chromium ready"

    # Create global wrapper
    info "Creating global command at $BIN_DIR..."
    mkdir -p "$BIN_DIR"

    cat > "$WRAPPER" << 'WRAPPER_EOF'
#!/usr/bin/env bash
# WboxCLI global wrapper — activates the managed venv transparently.
JOBCLI_VENV="$HOME/.jobcli/venv"

if [ ! -f "$JOBCLI_VENV/bin/python" ]; then
    echo "Error: WboxCLI installation not found at $JOBCLI_VENV"
    echo "Re-install with: curl -fsSL https://raw.githubusercontent.com/WhiteboxHub/project-ashby-automation/main/scripts/install.sh | bash"
    exit 1
fi

exec "$JOBCLI_VENV/bin/wboxcli" "$@"
WRAPPER_EOF
    chmod +x "$WRAPPER"
    ok "Command created: wboxcli"

    # Ensure PATH
    _add_to_path() {
        local profile_file="$1"
        local marker='# WboxCLI PATH'

        if [ -f "$profile_file" ] && grep -q "$marker" "$profile_file" 2>/dev/null; then
            return 0
        fi

        echo "" >> "$profile_file"
        echo "$marker" >> "$profile_file"
        echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$profile_file"
        return 1
    }

    if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
        info "Adding ~/.local/bin to PATH..."
        SHELL_NAME=$(basename "$SHELL")
        case "$SHELL_NAME" in
            zsh)  _add_to_path "$HOME/.zshrc" || true; ok "Updated ~/.zshrc" ;;
            bash)
                _add_to_path "$HOME/.bashrc" || true
                [ "$(uname)" = "Darwin" ] && _add_to_path "$HOME/.bash_profile" || true
                ok "Updated shell profile"
                ;;
            *)    warn "Unknown shell ($SHELL_NAME). Add manually: export PATH=\"\$HOME/.local/bin:\$PATH\"" ;;
        esac
    else
        ok "~/.local/bin already on PATH"
    fi

    # Done
    echo ""
    echo -e "${BOLD}${GREEN}╔══════════════════════════════════════╗${NC}"
    echo -e "${BOLD}${GREEN}║       Installation Complete! 🎉      ║${NC}"
    echo -e "${BOLD}${GREEN}╚══════════════════════════════════════╝${NC}"
    echo ""
    echo -e "  Install dir  : ${CYAN}$INSTALL_DIR${NC}"
    echo -e "  Command      : ${CYAN}wboxcli${NC}"
    echo ""
    echo -e "  ${BOLD}Next steps:${NC}"
    echo -e "    1. Restart your terminal (or source ~/.zshrc)"
    echo -e "    2. Type: ${CYAN}wboxcli${NC}"
    echo ""
}

# ══════════════════════════════════════════════════════════════════════
#  UPDATE
# ══════════════════════════════════════════════════════════════════════
cmd_update() {
    header "Update                 ║"
    _ensure_installed

    if [ -d "$SRC_DIR/.git" ]; then
        if [ -z "${JOBCLI_BRANCH:-}" ]; then
            BRANCH=$(git -C "$SRC_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "main")
        fi
    fi

    info "Pulling latest code from $BRANCH..."
    if [ -d "$SRC_DIR/.git" ]; then
        git -C "$SRC_DIR" fetch origin "$BRANCH" --depth 1 --quiet
        git -C "$SRC_DIR" checkout -B "$BRANCH" FETCH_HEAD --quiet
        ok "Source updated to latest $BRANCH"
    else
        fail "Source directory not found at $SRC_DIR. Run: $0 install"
    fi

    # Update TalentScreen extension
    EXT_TMP_DIR="/tmp/wboxcli_ext_clone_$$"
    EXT_URL="https://github.com/WhiteboxHub/project-talentscreen-autofill-extension.git"

    info "Updating TalentScreen extension..."
    git clone --depth 1 "$EXT_URL" "$EXT_TMP_DIR" --quiet 2>/dev/null || warn "Could not fetch latest extension"
    if [ -d "$EXT_TMP_DIR" ]; then
        mkdir -p "$SRC_DIR/bin"
        rm -rf "$SRC_DIR/bin/project-talentscreen-autofill-extension"
        mv "$EXT_TMP_DIR" "$SRC_DIR/bin/project-talentscreen-autofill-extension"
        ok "Extension updated"
    fi

    info "Reinstalling dependencies..."
    "$VENV_DIR/bin/pip" install --upgrade pip --quiet 2>/dev/null
    "$VENV_DIR/bin/pip" install -e "$SRC_DIR" --quiet 2>/dev/null
    ok "Dependencies updated"

    info "Updating Playwright Chromium..."
    "$VENV_DIR/bin/python" -m playwright install chromium --quiet 2>/dev/null || \
        "$VENV_DIR/bin/python" -m playwright install chromium 2>/dev/null || \
        warn "Playwright update had issues — run 'wboxcli doctor' to verify."
    ok "Playwright updated"

    echo ""
    echo -e "${GREEN}✓ WboxCLI updated successfully.${NC}"
    echo -e "  Branch: ${CYAN}$BRANCH${NC}"
    echo ""
}

# ══════════════════════════════════════════════════════════════════════
#  UNINSTALL
# ══════════════════════════════════════════════════════════════════════
cmd_uninstall() {
    header "Uninstaller            ║"

    read -r -p "This will delete $INSTALL_DIR and the wboxcli shim in $BIN_DIR. Continue? [y/N] " confirm
    if [[ ! "$confirm" =~ ^[Yy]$ ]]; then
        echo -e "${YELLOW}Cancelled.${NC}"
        exit 0
    fi

    # Remove wrapper
    if [ -f "$WRAPPER" ] || [ -L "$WRAPPER" ]; then
        rm -f "$WRAPPER"
        ok "Removed $WRAPPER"
    else
        warn "Not found: $WRAPPER"
    fi

    # Remove install dir
    if [ -d "$INSTALL_DIR" ]; then
        rm -rf "$INSTALL_DIR"
        ok "Removed $INSTALL_DIR"
    else
        warn "Install directory not found at $INSTALL_DIR"
    fi

    echo ""
    echo -e "${GREEN}WboxCLI has been uninstalled.${NC}"
    echo -e "${YELLOW}Note:${NC} The PATH entry in your shell profile was left intact — remove it manually if you wish."
    echo ""
}

# ══════════════════════════════════════════════════════════════════════
#  RESET (clear login / API keys / resume — jobs kept)
# ══════════════════════════════════════════════════════════════════════
cmd_reset() {
    header "Reset & Re-Setup       ║"
    _ensure_installed

    info "Clearing login, LLM keys, and resume (jobs are kept)..."
    "$(_venv_python)" -m jobcli.cli.main reset "$@" || return

    ok "Configuration cleared — starting setup wizard"
    echo ""
    if [ -f "$WRAPPER" ]; then
        exec "$WRAPPER"
    elif [ -f "$VENV_DIR/bin/wboxcli" ]; then
        exec "$VENV_DIR/bin/wboxcli"
    else
        exec "$(_venv_python)" -m jobcli.cli.entry
    fi
}

# ══════════════════════════════════════════════════════════════════════
#  CLEAR-JOBS (remove jobs only, keep credentials/resume)
# ══════════════════════════════════════════════════════════════════════
cmd_clear_jobs() {
    header "Clear Jobs             ║"
    _ensure_installed

    local force=false
    if [ "${1:-}" = "--force" ] || [ "${1:-}" = "-f" ]; then
        force=true
    fi

    if [ "$force" != true ]; then
        read -r -p "Delete all discovered jobs and application logs? (Credentials, resume, and memory are kept.) [y/N] " confirm
        if [[ ! "$confirm" =~ ^[Yy]$ ]]; then
            echo -e "${YELLOW}Cancelled.${NC}"
            return
        fi
    fi

    info "Clearing job data via wboxcli..."
    "$(_venv_python)" -m jobcli.cli.main db clear-jobs --force
    ok "Job data cleared"
    echo ""
}

# ══════════════════════════════════════════════════════════════════════
#  SETUP (re-run onboarding without wiping data)
# ══════════════════════════════════════════════════════════════════════
cmd_setup() {
    header "Setup Wizard           ║"
    _ensure_installed

    info "Launching the setup wizard (re-enter credentials, LLM key, resume)..."
    echo ""

    # The TUI's "setup" command calls _run_onboarding(force=True)
    # which walks through all 4 steps regardless of current state.
    if [ -f "$WRAPPER" ]; then
        exec "$WRAPPER" setup
    elif [ -f "$VENV_DIR/bin/wboxcli" ]; then
        exec "$VENV_DIR/bin/wboxcli" setup
    else
        "$(_venv_python)" -m jobcli.cli.main setup
    fi
}

# ══════════════════════════════════════════════════════════════════════
#  DOCTOR (health checks)
# ══════════════════════════════════════════════════════════════════════
cmd_doctor() {
    header "Health Check           ║"
    _ensure_installed

    info "Running diagnostics..."
    echo ""
    "$(_venv_python)" -m jobcli.cli.main doctor
}

# ══════════════════════════════════════════════════════════════════════
#  STATUS (show config summary)
# ══════════════════════════════════════════════════════════════════════
cmd_status() {
    header "Status                 ║"

    echo -e "  ${DIM}Install dir${NC}   : ${CYAN}$INSTALL_DIR${NC}"

    if [ -d "$INSTALL_DIR" ]; then
        ok "Installation found"
    else
        warn "Not installed"
        return
    fi

    if [ -f "$VENV_DIR/bin/python" ]; then
        ok "Python venv ready"
    else
        warn "Python venv missing"
    fi

    if [ -f "$DB_FILE" ]; then
        local db_size
        db_size=$(du -h "$DB_FILE" 2>/dev/null | cut -f1)
        ok "Database exists ($db_size)"
    else
        warn "No database found (run 'wboxcli' to set up)"
    fi

    if [ -f "$WRAPPER" ]; then
        ok "Global command: wboxcli"
    else
        warn "Global command not installed"
    fi

    local ext_dir="$INSTALL_DIR/extension_unpacked"
    if [ -d "$ext_dir" ] && [ -f "$ext_dir/manifest.json" ]; then
        ok "TalentScreen extension installed"
    else
        warn "Extension not unpacked (run 'wboxcli setup')"
    fi

    if [ -d "$SRC_DIR/.git" ]; then
        local branch_name
        branch_name=$(git -C "$SRC_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "unknown")
        local commit_hash
        commit_hash=$(git -C "$SRC_DIR" rev-parse --short HEAD 2>/dev/null || echo "unknown")
        echo -e "  ${DIM}Branch${NC}        : ${CYAN}$branch_name${NC} (${commit_hash})"
    fi

    echo ""
}

# ══════════════════════════════════════════════════════════════════════
#  HELP
# ══════════════════════════════════════════════════════════════════════
cmd_help() {
    echo ""
    echo -e "${BOLD}${CYAN}WboxCLI Management Script${NC}"
    echo ""
    echo -e "  ${BOLD}Usage:${NC}  $0 <command> [options]"
    echo ""
    echo -e "  ${BOLD}Commands:${NC}"
    echo -e "    ${CYAN}install${NC}       Install WboxCLI globally (clone, venv, Playwright, shim)"
    echo -e "    ${CYAN}setup${NC}         Re-run the initial setup wizard (credentials, LLM, resume)"
    echo -e "    ${CYAN}update${NC}        Pull latest code and reinstall dependencies"
    echo -e "    ${CYAN}uninstall${NC}     Remove everything (config, venv, shim)"
    echo -e "    ${CYAN}reset${NC}         Clear login, API keys, resume (jobs kept) and re-run setup"
    echo -e "    ${CYAN}clear-jobs${NC}    Delete discovered jobs only (keeps credentials/resume)"
    echo -e "    ${CYAN}doctor${NC}        Run environment health checks"
    echo -e "    ${CYAN}status${NC}        Show current installation status"
    echo -e "    ${CYAN}help${NC}          Show this help message"
    echo ""
    echo -e "  ${BOLD}Options:${NC}"
    echo -e "    ${DIM}--force, -f${NC}   Skip confirmation prompts (for reset, clear-jobs)"
    echo ""
    echo -e "  ${BOLD}Examples:${NC}"
    echo -e "    $0 install              # Fresh install"
    echo -e "    $0 setup                # Re-run initial setup wizard"
    echo -e "    $0 update               # Pull latest + reinstall deps"
    echo -e "    $0 reset                # Clear login/keys/resume + re-run setup"
    echo -e "    $0 reset --force        # Same, skip confirmation"
    echo -e "    $0 clear-jobs           # Clear jobs, keep credentials"
    echo -e "    $0 uninstall            # Full removal"
    echo ""
    echo -e "  ${BOLD}Environment:${NC}"
    echo -e "    ${DIM}JOBCLI_BRANCH${NC}  Override branch (default: main). Example:"
    echo -e "                   JOBCLI_BRANCH=dev $0 install"
    echo ""
}

# ══════════════════════════════════════════════════════════════════════
#  MAIN DISPATCH
# ══════════════════════════════════════════════════════════════════════
COMMAND="${1:-help}"
shift || true

case "$COMMAND" in
    install)     cmd_install "$@" ;;
    setup)       cmd_setup "$@" ;;
    update)      cmd_update "$@" ;;
    uninstall)   cmd_uninstall "$@" ;;
    reset)       cmd_reset "$@" ;;
    clear-jobs)  cmd_clear_jobs "$@" ;;
    doctor)      cmd_doctor "$@" ;;
    status)      cmd_status "$@" ;;
    help|--help|-h)  cmd_help ;;
    *)
        echo -e "${RED}Unknown command: $COMMAND${NC}"
        echo ""
        cmd_help
        exit 1
        ;;
esac
