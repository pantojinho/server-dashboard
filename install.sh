#!/usr/bin/env bash
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Hermes Server Dashboard — Installation Script
#  Works on any Linux system with Python 3.10+
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"
CONFIG_FILE="$SCRIPT_DIR/config.yaml"
SERVICE_NAME="server-dashboard"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
USER_SERVICE_FILE="$HOME/.config/systemd/user/${SERVICE_NAME}.service"

# Colors
G='\033[0;32m'; Y='\033[1;33m'; R='\033[0;31m'; C='\033[0;36m'; N='\033[0m'

info()  { echo -e "${C}[INFO]${N} $*"; }
ok()    { echo -e "${G}[OK]${N} $*"; }
warn()  { echo -e "${Y}[WARN]${N} $*"; }
err()   { echo -e "${R}[ERROR]${N} $*"; }

# ── Pre-flight checks ─────────────────────────────────────────
info "Checking prerequisites..."

if ! command -v python3 &>/dev/null; then
    err "Python 3 is not installed. Install it first:"
    echo "  sudo apt install python3 python3-venv python3-pip"
    exit 1
fi

PYTHON_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
PYTHON_MAJOR=$(echo "$PYTHON_VERSION" | cut -d. -f1)
PYTHON_MINOR=$(echo "$PYTHON_VERSION" | cut -d. -f2)

if [ "$PYTHON_MAJOR" -lt 3 ] || ([ "$PYTHON_MAJOR" -eq 3 ] && [ "$PYTHON_MINOR" -lt 10 ]); then
    err "Python 3.10+ required. Found: $PYTHON_VERSION"
    exit 1
fi

ok "Python $PYTHON_VERSION found"

# ── Create virtual environment ─────────────────────────────────
if [ ! -d "$VENV_DIR" ]; then
    info "Creating Python virtual environment..."
    python3 -m venv "$VENV_DIR"
    ok "Virtual environment created at .venv/"
else
    ok "Virtual environment already exists"
fi

# ── Install dependencies ───────────────────────────────────────
info "Installing Python dependencies..."
"$VENV_DIR/bin/pip" install --upgrade pip --quiet
"$VENV_DIR/bin/pip" install -r "$SCRIPT_DIR/requirements.txt" --quiet
ok "Dependencies installed"

# ── Create config.yaml from example ────────────────────────────
if [ ! -f "$CONFIG_FILE" ]; then
    info "Creating config.yaml from config.example.yaml..."
    cp "$SCRIPT_DIR/config.example.yaml" "$CONFIG_FILE"
    warn "Review and edit config.yaml before running!"
    warn "Key settings to check: server.port, services, integrations"
else
    ok "config.yaml already exists (not overwritten)"
fi

# ── Detect dashboard port ─────────────────────────────────────
DASHBOARD_PORT=$(python3 -c "
import yaml
try:
    with open('$CONFIG_FILE') as f:
        c = yaml.safe_load(f) or {}
    print(c.get('server', {}).get('port', 18791))
except:
    print(18791)
")

# ── Ask for installation type ──────────────────────────────────
echo ""
echo -e "${Y}━━━ Installation Type ━━━${N}"
echo "  1) System service (requires sudo, starts on boot)"
echo "  2) User service (no sudo, starts on login)"
echo "  3) Run directly (no service, manual start)"
echo ""
read -rp "Choose [1/2/3] (default: 2): " INSTALL_TYPE
INSTALL_TYPE="${INSTALL_TYPE:-2}"

# ── Generate service file ──────────────────────────────────────
generate_service() {
    local SCOPE="$1"
    local DEST="$2"

    cat > "$DEST" <<SERVICEEOF
[Unit]
Description=Hermes Server Dashboard
After=network.target

[Service]
Type=simple
WorkingDirectory=$SCRIPT_DIR
ExecStart=$VENV_DIR/bin/python3 $SCRIPT_DIR/server.py
Restart=on-failure
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
SERVICEEOF

    if [ "$SCOPE" = "system" ]; then
        echo "WantedBy=multi-user.target" >> "$DEST"
    else
        echo "WantedBy=default.target" >> "$DEST"
    fi
}

case "$INSTALL_TYPE" in
    1)
        info "Installing as system service..."
        generate_service "system" "/tmp/${SERVICE_NAME}.service"
        sudo cp "/tmp/${SERVICE_NAME}.service" "$SERVICE_FILE"
        sudo sed -i "s|^ExecStart=.*|ExecStart=$VENV_DIR/bin/python3 $SCRIPT_DIR/server.py|" "$SERVICE_FILE"
        sudo systemctl daemon-reload
        sudo systemctl enable "$SERVICE_NAME"
        sudo systemctl start "$SERVICE_NAME"
        ok "System service installed and started"
        echo ""
        echo "  Commands:"
        echo "    sudo systemctl status $SERVICE_NAME"
        echo "    sudo systemctl restart $SERVICE_NAME"
        echo "    sudo systemctl stop $SERVICE_NAME"
        echo "    sudo journalctl -u $SERVICE_NAME -f"
        ;;
    2)
        info "Installing as user service..."
        mkdir -p "$HOME/.config/systemd/user/"
        generate_service "user" "$USER_SERVICE_FILE"
        systemctl --user daemon-reload
        systemctl --user enable "$SERVICE_NAME"
        systemctl --user start "$SERVICE_NAME"
        ok "User service installed and started"
        echo ""
        echo "  Commands:"
        echo "    systemctl --user status $SERVICE_NAME"
        echo "    systemctl --user restart $SERVICE_NAME"
        echo "    systemctl --user stop $SERVICE_NAME"
        echo "    journalctl --user -u $SERVICE_NAME -f"
        ;;
    3)
        info "No service installation. To run manually:"
        echo ""
        echo "    cd $SCRIPT_DIR"
        echo "    .venv/bin/python3 server.py"
        ;;
esac

# ── Done ───────────────────────────────────────────────────────
echo ""
echo -e "${G}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${N}"
echo -e "${G}  ✓ Installation complete!${N}"
echo -e "${G}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${N}"
echo ""
echo "  Dashboard URL: http://localhost:$DASHBOARD_PORT"
echo "  Config file:   $CONFIG_FILE"
echo ""
echo "  Next steps:"
echo "    1. Edit config.yaml to match your setup"
echo "    2. Open the dashboard URL in a browser"
echo ""
