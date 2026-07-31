#!/bin/bash
# setup.sh — Configure a Raspberry Pi to monitor a SunPower SMS-PVS20R1 gateway.
#
# The Pi bridges two networks:
#   - WiFi (wlan0): Main home network (DHCP, default gateway)
#   - Ethernet (eth0): PVS20R1 LAN2 port (static IP 172.27.153.254/24, no gateway)
#
# Run as root on a Pi with WiFi already connected to your network.
#
# Usage:
#   sudo bash setup.sh

set -e

echo "=== PVS20R1 Monitor Setup ==="
echo

if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: Run as root (sudo)."
    exit 1
fi

if ! which python3 >/dev/null 2>&1 && [ ! -f /usr/bin/python3 ]; then
    echo "ERROR: python3 not found."
    exit 1
fi

PYTHON_VERSION=$(python3 --version 2>&1)
echo "Python: $PYTHON_VERSION"

# ── Network configuration ────────────────────────────────────────────────────

echo
echo "Configuring eth0 for PVS20R1 LAN2..."

# Remove existing IP if present
ip addr del 172.27.153.254/24 dev eth0 2>/dev/null || true
ip addr add 172.27.153.254/24 dev eth0
echo "  eth0: 172.27.153.254/24 (no gateway, WiFi is default route)"

# Persist via dhcpcd
if [ -f /etc/dhcpcd.conf ]; then
    if ! grep -q "interface eth0" /etc/dhcpcd.conf 2>/dev/null; then
        cat >> /etc/dhcpcd.conf <<'EOF'

# PVS20R1 LAN2 — static IP, no gateway (WiFi is the default route)
interface eth0
static ip_address=172.27.153.254/24
noipv4LL
EOF
        echo "  Persisted to /etc/dhcpcd.conf"
    else
        echo "  Already in /etc/dhcpcd.conf"
    fi
else
    echo "  WARNING: No dhcpcd.conf — IP will not persist across reboots"
fi

# ── Deploy service ───────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CURRENT_USER="${SUDO_USER:-$(whoami)}"

echo
echo "Deploying systemd service..."

# Fill in the service template
INSTALL_DIR="$SCRIPT_DIR"
CURRENT_USER="${SUDO_USER:-pi}"

sed "s|REPLACE_DIR|${INSTALL_DIR}|g" "${INSTALL_DIR}/service/pvs20r1-monitor.service" | \
sed "s|REPLACE_USER|${CURRENT_USER}|g" | \
sudo tee /etc/systemd/system/pvs20r1-monitor.service > /dev/null

systemctl daemon-reload
systemctl enable pvs20r1-monitor 2>/dev/null || true
echo "  Service: pvs20r1-monitor"

# Create logs directory and fix ownership
mkdir -p "${INSTALL_DIR}/logs"
chown -R "${CURRENT_USER}:${CURRENT_USER}" "${INSTALL_DIR}"
echo "  Logs: ${INSTALL_DIR}/logs/monitor.log"

# ── Test ──────────────────────────────────────────────────────────────────────

echo
echo "Testing gateway connection..."

if ping -c 2 -W 3 172.27.153.1 &>/dev/null; then
    echo "  Gateway responds to ping"
else
    echo "  Gateway didn't respond to ping — trying HTTP..."
fi

echo
echo "Starting monitor for a quick test..."
echo "  (Press Ctrl+C to stop — the service will handle production)"
echo
exec python3 "${INSTALL_DIR}/pvs20r1_monitor.py" --poll-interval 60