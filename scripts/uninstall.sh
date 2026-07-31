#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────
#  WboxCLI Global Uninstaller
#
#  Usage:
#    curl -fsSL https://raw.githubusercontent.com/WhiteboxHub/project-ashby-automation/main/scripts/uninstall.sh | bash
#    — or —
#    bash ~/.jobcli/src/scripts/uninstall.sh
# ──────────────────────────────────────────────────────────────────────

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

INSTALL_DIR="$HOME/.jobcli"
BIN_DIR="$HOME/.local/bin"
WRAPPERS=("$BIN_DIR/wboxcli")

echo ""
echo -e "${BOLD}${RED}WboxCLI — Uninstaller${NC}"
echo ""

# Confirm
read -r -p "This will delete $INSTALL_DIR and the wboxcli shim in $BIN_DIR. Continue? [y/N] " confirm
if [[ ! "$confirm" =~ ^[Yy]$ ]]; then
    echo -e "${YELLOW}Cancelled.${NC}"
    exit 0
fi

# Remove wrappers
for w in "${WRAPPERS[@]}"; do
    if [ -f "$w" ] || [ -L "$w" ]; then
        rm -f "$w"
        echo -e "${GREEN}[✓]${NC} Removed $w"
    else
        echo -e "${YELLOW}[—]${NC} Not found: $w"
    fi
done

# Remove install dir
if [ -d "$INSTALL_DIR" ]; then
    rm -rf "$INSTALL_DIR"
    echo -e "${GREEN}[✓]${NC} Removed $INSTALL_DIR"
else
    echo -e "${YELLOW}[—]${NC} Install directory not found at $INSTALL_DIR"
fi

echo ""
echo -e "${GREEN}WboxCLI has been uninstalled.${NC}"
echo -e "${YELLOW}Note:${NC} The PATH entry in your shell profile (~/.zshrc or ~/.bashrc) was left intact — remove it manually if you wish."
echo ""
