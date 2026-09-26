#!/usr/bin/env bash
# install_camera_server.sh
# Sets up the .venv_camera venv for running the composed camera server
# on the robot computer.
#
# Installs gear_sonic[camera] which includes the ZMQ-based camera server
# framework and the depthai SDK (OAK cameras). Selecting RealSense during
# service configuration also installs the pyrealsense2 SDK.
#
# Usage:  bash install_scripts/install_camera_server.sh   (run from repo root)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# ── 0. Print detected architecture ───────────────────────────────────────────
ARCH="$(uname -m)"
echo "[OK] Architecture: $ARCH"

# ── 1. Ensure uv is installed and available ──────────────────────────────────
if ! command -v uv &>/dev/null; then
    echo "[INFO] uv not found – installing via official installer …"
    curl -LsSf https://astral.sh/uv/install.sh | sh

    if [ -f "$HOME/.local/bin/env" ]; then
        # shellcheck disable=SC1091
        source "$HOME/.local/bin/env"
    elif [ -f "$HOME/.cargo/env" ]; then
        # shellcheck disable=SC1091
        source "$HOME/.cargo/env"
    else
        export PATH="$HOME/.local/bin:$PATH"
    fi

    if ! command -v uv &>/dev/null; then
        echo "[ERROR] uv installation succeeded but binary not found on PATH."
        echo "        Please add ~/.local/bin (or ~/.cargo/bin) to your PATH and re-run."
        exit 1
    fi
fi
echo "[OK] uv $(uv --version)"

# ── 2. Install a uv-managed Python 3.10 ─────────────────────────────────────
echo "[INFO] Installing uv-managed Python 3.10 …"
uv python install 3.10
MANAGED_PY="$(uv python find --no-project 3.10)"
echo "[OK] Using Python: $MANAGED_PY"

# ── 3. Clean previous venv (if any) ─────────────────────────────────────────
cd "$REPO_ROOT"
echo "[INFO] Removing old .venv_camera (if present) …"
rm -rf .venv_camera

# ── 4. Create venv & install camera extra ────────────────────────────────────
echo "[INFO] Creating .venv_camera with uv-managed Python 3.10 …"
uv venv .venv_camera --python "$MANAGED_PY" --prompt gear_sonic_camera
# shellcheck disable=SC1091
source .venv_camera/bin/activate
echo "[INFO] Installing gear_sonic[camera] …"
uv pip install -e "gear_sonic[camera]"

echo ""
echo "══════════════════════════════════════════════════════════════"
echo "  Camera server venv setup complete!"
echo "  depthai (OAK cameras) is included by default."
echo ""
echo "  Activate the venv with:"
echo "    source .venv_camera/bin/activate"
echo ""
echo "  RealSense SDK is installed when selected during service setup."
echo "  For manual RealSense setup, install into the venv:"
echo '    uv pip install -e "gear_sonic[camera,realsense]"'
echo ""
echo "  See docs/source/tutorials/data_collection.md for full setup."
echo "══════════════════════════════════════════════════════════════"

# ── 5. Optionally install the systemd service ────────────────────────────────
SERVICE_TEMPLATE="$REPO_ROOT/systemd/composed_camera_server.service"
SERVICE_NAME="composed_camera_server.service"

if [ ! -f "$SERVICE_TEMPLATE" ]; then
    echo ""
    echo "[WARN] systemd template not found at $SERVICE_TEMPLATE — skipping."
    exit 0
fi

echo ""
read -rp "Install the camera server as a systemd service (auto-start on boot)? [y/N] " INSTALL_SERVICE
if [[ ! "$INSTALL_SERVICE" =~ ^[Yy]$ ]]; then
    echo ""
    echo "  Skipped systemd install. You can run the camera server manually:"
    echo "    source .venv_camera/bin/activate"
    echo "    python -m gear_sonic.camera.composed_camera --ego-view-camera oak --port 5555"
    echo ""
    exit 0
fi

# Gather configuration
echo ""
echo "── Camera service configuration ──"
echo "  Each camera needs a type and a device ID so the server knows"
echo "  which physical camera maps to each mount position (ego, wrist, etc.)."
echo ""

detect_oak_cameras() {
    "${REPO_ROOT}/.venv_camera/bin/python" -c "
import depthai as dai
devices = dai.Device.getAllAvailableDevices()
if not devices:
    exit(1)
for i, d in enumerate(devices):
    # Try to get actual MxID; fall back to string representation
    mxid = None
    for attr in ['mxid', 'getMxId']:
        if hasattr(d, attr):
            val = getattr(d, attr)
            mxid = val() if callable(val) else val
            if mxid:
                break
    state = d.state.name if hasattr(d, 'state') else 'N/A'
    name = getattr(d, 'name', '')
    if mxid and mxid != name:
        print(f'    [{i}] MxId: {mxid}  port: {name}  state: {state}')
    else:
        # MxID not available; show all useful attributes
        print(f'    [{i}] device: {d}  state: {state}')
        print(f'         attributes: {[a for a in dir(d) if not a.startswith(\"_\")]}')
" 2>&1
}

detect_realsense_cameras() {
    "${REPO_ROOT}/.venv_camera/bin/python" -c "
import pyrealsense2 as rs
devices = rs.context().query_devices()
if not devices:
    exit(1)
for device in devices:
    print(f'    Serial: {device.get_info(rs.camera_info.serial_number)}  '
          f'name: {device.get_info(rs.camera_info.name)}')
" 2>&1
}

OAK_DETECTED=false
REALSENSE_DETECTED=false
show_camera_devices() {
    local camera_type="$1" detector camera_label devices found retry
    case "$camera_type" in
        oak|oak_mono)
            if $OAK_DETECTED; then return; fi
            OAK_DETECTED=true
            detector=detect_oak_cameras
            camera_label=OAK
            ;;
        realsense)
            if $REALSENSE_DETECTED; then return; fi
            echo '[INFO] Installing gear_sonic[camera,realsense] …'
            uv pip install -e "gear_sonic[camera,realsense]"
            REALSENSE_DETECTED=true
            detector=detect_realsense_cameras
            camera_label=RealSense
            echo "  Use serial numbers for device IDs when multiple RealSense cameras are connected."
            ;;
        usb) return ;;
        *) echo "[ERROR] Unknown camera type: $camera_type"; exit 1 ;;
    esac

    while true; do
        echo "  Detecting connected $camera_label cameras …"
        devices="$($detector)" && found=true || found=false
        if $found && [ -n "$devices" ]; then
            echo "$devices"
            break
        fi
        echo "  (no $camera_label devices detected)"
        if [ -n "$devices" ]; then
            echo "  SDK output: $devices"
        fi
        read -rp "  Retry detection? [Y/n] (or 'n' to enter device IDs manually): " retry
        if [[ "$retry" =~ ^[Nn]$ ]]; then break; fi
    done
}

# Build ExecStart args incrementally
CAMERA_ARGS=""

# --- Ego-view camera (required) ---
read -rp "  Ego-view camera type (oak, oak_mono, realsense, usb) [oak]: " EGO_TYPE
EGO_TYPE="${EGO_TYPE:-oak}"
show_camera_devices "$EGO_TYPE"
read -rp "  Ego-view device ID (OAK MxID, RealSense serial, or USB index): " EGO_DEVICE_ID
CAMERA_ARGS="--ego-view-camera ${EGO_TYPE}"
if [ -n "$EGO_DEVICE_ID" ]; then
    CAMERA_ARGS="${CAMERA_ARGS} --ego-view-device-id ${EGO_DEVICE_ID}"
fi

# --- Left wrist camera (optional) ---
echo ""
read -rp "  Add a left-wrist camera? [y/N]: " ADD_LEFT
if [[ "$ADD_LEFT" =~ ^[Yy]$ ]]; then
    read -rp "  Left-wrist camera type (oak, oak_mono, realsense, usb) [oak]: " LEFT_TYPE
    LEFT_TYPE="${LEFT_TYPE:-oak}"
    show_camera_devices "$LEFT_TYPE"
    read -rp "  Left-wrist device ID (OAK MxID, RealSense serial, or USB index): " LEFT_DEVICE_ID
    CAMERA_ARGS="${CAMERA_ARGS} --left-wrist-camera ${LEFT_TYPE}"
    if [ -n "$LEFT_DEVICE_ID" ]; then
        CAMERA_ARGS="${CAMERA_ARGS} --left-wrist-device-id ${LEFT_DEVICE_ID}"
    fi
fi

# --- Right wrist camera (optional) ---
echo ""
read -rp "  Add a right-wrist camera? [y/N]: " ADD_RIGHT
if [[ "$ADD_RIGHT" =~ ^[Yy]$ ]]; then
    read -rp "  Right-wrist camera type (oak, oak_mono, realsense, usb) [oak]: " RIGHT_TYPE
    RIGHT_TYPE="${RIGHT_TYPE:-oak}"
    show_camera_devices "$RIGHT_TYPE"
    read -rp "  Right-wrist device ID (OAK MxID, RealSense serial, or USB index): " RIGHT_DEVICE_ID
    CAMERA_ARGS="${CAMERA_ARGS} --right-wrist-camera ${RIGHT_TYPE}"
    if [ -n "$RIGHT_DEVICE_ID" ]; then
        CAMERA_ARGS="${CAMERA_ARGS} --right-wrist-device-id ${RIGHT_DEVICE_ID}"
    fi
fi

echo ""
read -rp "  ZMQ port [5555]: " CFG_PORT
CFG_PORT="${CFG_PORT:-5555}"
CAMERA_ARGS="${CAMERA_ARGS} --port ${CFG_PORT}"

EXEC_START="${REPO_ROOT}/.venv_camera/bin/python -m gear_sonic.camera.composed_camera ${CAMERA_ARGS}"
echo ""
echo "  ExecStart command:"
echo "    $EXEC_START"
echo ""
read -rp "  Look correct? [Y/n]: " CONFIRM
if [[ "$CONFIRM" =~ ^[Nn]$ ]]; then
    echo "  Aborted. Edit systemd/composed_camera_server.service manually."
    exit 0
fi

# Generate unit file directly (avoids fragile sed on multi-line ExecStart)
TMPUNIT="$(mktemp)"
cat > "$TMPUNIT" <<UNIT
[Unit]
Description=SONIC Composed Camera Server (ZMQ)
After=network.target

[Service]
Type=simple
User=$USER
Environment="HOME=$HOME"
Environment="REPO_DIR=$REPO_ROOT"
WorkingDirectory=$REPO_ROOT
ExecStart=$EXEC_START
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
UNIT

echo ""
echo "[INFO] Installing systemd service …"
sudo cp "$TMPUNIT" "/etc/systemd/system/$SERVICE_NAME"
rm -f "$TMPUNIT"

sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"
sudo systemctl start "$SERVICE_NAME"

echo ""
echo "══════════════════════════════════════════════════════════════"
echo "  systemd service installed and started!"
echo ""
echo "  Check status:"
echo "    sudo systemctl status $SERVICE_NAME"
echo ""
echo "  View logs:"
echo "    journalctl -u $SERVICE_NAME -f"
echo ""
echo "  To reconfigure, edit and re-run this script, or:"
echo "    sudo systemctl edit $SERVICE_NAME"
echo "    sudo systemctl restart $SERVICE_NAME"
echo "══════════════════════════════════════════════════════════════"
