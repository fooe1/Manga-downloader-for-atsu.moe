#!/usr/bin/env bash
# =============================================================================
# Manga Downloader – Linux / macOS Installer
# =============================================================================
# How to run:
#   chmod +x install.sh
#   ./install.sh
# =============================================================================

set -e  # stop on any error

# ── Colours ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

info()    { echo -e "${CYAN}[•] $*${RESET}"; }
success() { echo -e "${GREEN}[✓] $*${RESET}"; }
warn()    { echo -e "${YELLOW}[!] $*${RESET}"; }
error()   { echo -e "${RED}[✗] $*${RESET}"; exit 1; }

# ── Banner ────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}╔══════════════════════════════════════════╗${RESET}"
echo -e "${BOLD}║       Manga Downloader  –  Installer     ║${RESET}"
echo -e "${BOLD}╚══════════════════════════════════════════╝${RESET}"
echo ""

# ── Step 1: Check Python ──────────────────────────────────────────────────────
info "Checking for Python 3.11 or newer..."

PYTHON=""
for cmd in python3 python3.13 python3.12 python3.11 python; do
    if command -v "$cmd" &>/dev/null; then
        VER=$("$cmd" -c "import sys; print(sys.version_info >= (3,11))" 2>/dev/null)
        if [ "$VER" = "True" ]; then
            PYTHON="$cmd"
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    error "Python 3.11 or newer is required but was not found.

  Linux (Debian/Ubuntu/Arch):
    sudo apt install python3        # Debian/Ubuntu
    sudo pacman -S python           # Arch

  macOS:
    brew install python@3.12        # requires Homebrew (brew.sh)

After installing Python, run this script again."
fi

PY_VER=$("$PYTHON" --version 2>&1)
success "Found $PY_VER  ($PYTHON)"

# ── Step 2: Create virtual environment ───────────────────────────────────────
VENV_DIR=".venv"
if [ -d "$VENV_DIR" ]; then
    warn "Virtual environment already exists – skipping creation."
else
    info "Creating virtual environment in .venv/ ..."
    "$PYTHON" -m venv "$VENV_DIR"
    success "Virtual environment created."
fi

# Activate it for the rest of the install
source "$VENV_DIR/bin/activate"

# ── Step 3: Install Python dependencies ──────────────────────────────────────
info "Installing Python packages (playwright, flask, requests)..."
pip install --upgrade pip --quiet
pip install -r requirements.txt --quiet
success "Python packages installed."

# ── Step 4: Install Chromium browser for Playwright ──────────────────────────
info "Installing Chromium browser (this may take a minute)..."
playwright install chromium
success "Chromium installed."

# ── Step 5: Create launcher script ───────────────────────────────────────────
LAUNCHER="Start Manga Downloader.sh"
cat > "$LAUNCHER" << 'LAUNCHER_EOF'
#!/usr/bin/env bash
# Manga Downloader launcher
cd "$(dirname "$0")"
source .venv/bin/activate
echo ""
echo "  Starting Manga Downloader..."
echo "  Open your browser at: http://localhost:7337"
echo "  Press Ctrl+C here to stop."
echo ""
python app.py
LAUNCHER_EOF

chmod +x "$LAUNCHER"
success "Launcher created: '$LAUNCHER'"

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}${GREEN}══════════════════════════════════════════${RESET}"
echo -e "${BOLD}${GREEN}  Installation complete!${RESET}"
echo -e "${BOLD}${GREEN}══════════════════════════════════════════${RESET}"
echo ""
echo -e "  To start the app, run:"
echo -e "    ${CYAN}./'${LAUNCHER}'${RESET}"
echo ""
echo -e "  Or from the terminal:"
echo -e "    ${CYAN}source .venv/bin/activate && python app.py${RESET}"
echo ""
echo -e "  Then open ${BOLD}http://localhost:7337${RESET} in your browser."
echo ""
