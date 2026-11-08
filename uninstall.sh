#!/usr/bin/env bash
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "Run this uninstaller with sudo." >&2
    exit 1
fi

TARGET_USER="${SUDO_USER:-${PREDATOR_FAN_USER:-}}"

systemctl disable --now predator-fan-helper.service 2>/dev/null || true
rm -f /etc/systemd/system/predator-fan-helper.service
systemctl daemon-reload

rm -f /etc/modules-load.d/predator-fan-manager.conf
rm -f /etc/modprobe.d/predator-fan-manager.conf
rm -rf /usr/local/lib/predator-fan-manager
rm -f /usr/local/bin/predator-fan-manager /usr/local/bin/predator-probe
rm -f /usr/share/applications/predator-fan-manager.desktop

if [ -n "$TARGET_USER" ] && id "$TARGET_USER" >/dev/null 2>&1; then
    TARGET_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"
    rm -f "$TARGET_HOME/.config/autostart/predator-fan-manager.desktop"
fi

echo "Removed Predator Fan Manager. Settings were left in ~/.config/predator-fan-manager."
