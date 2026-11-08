#!/usr/bin/env bash
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "Run this installer with sudo." >&2
    exit 1
fi

PRODUCT="$(cat /sys/class/dmi/id/product_name 2>/dev/null || true)"
if [[ "$PRODUCT" != *PH315-51* ]]; then
    echo "This installer is only for the Acer Predator Helios 300 PH315-51." >&2
    echo "Detected model: ${PRODUCT:-unknown}" >&2
    exit 1
fi

TARGET_USER="${SUDO_USER:-${PREDATOR_FAN_USER:-}}"
if [ -z "$TARGET_USER" ] || ! id "$TARGET_USER" >/dev/null 2>&1; then
    echo "Could not determine the desktop user. Run with sudo from that user's session." >&2
    exit 1
fi

TARGET_UID="$(id -u "$TARGET_USER")"
TARGET_GID="$(id -g "$TARGET_USER")"
TARGET_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"
SOURCE="$(cd "$(dirname "$0")" && pwd)"
DEST="/usr/local/lib/predator-fan-manager"
UNIT="/etc/systemd/system/predator-fan-helper.service"

if ! python3 - <<'PY'
import gi
gi.require_version("Gtk", "3.0")
gi.require_version("AyatanaAppIndicator3", "0.1")
PY
then
    echo "Missing Python GTK/AppIndicator dependencies." >&2
    echo "Install: sudo apt install python3-gi gir1.2-gtk-3.0 gir1.2-ayatanaappindicator3-0.1" >&2
    exit 1
fi

cat > /etc/modules-load.d/predator-fan-manager.conf <<'EOF'
acpi_call
ec_sys
EOF
cat > /etc/modprobe.d/predator-fan-manager.conf <<'EOF'
options ec_sys write_support=1
EOF

modprobe acpi_call 2>/dev/null || true
if [ ! -e /sys/kernel/debug/ec/ec0/io ]; then
    modprobe -r ec_sys 2>/dev/null || true
    modprobe ec_sys write_support=1 2>/dev/null || true
fi

if [ ! -e /proc/acpi/call ] && [ ! -e /sys/kernel/debug/ec/ec0/io ]; then
    echo "Neither acpi_call nor ec_sys is available." >&2
    echo "Install the ACPI module first:" >&2
    echo "  sudo apt install acpi-call-dkms" >&2
    echo "If Secure Boot is enabled, enroll the DKMS signing key before retrying." >&2
    exit 1
fi

install -d -m 0755 "$DEST"
install -m 0644 "$SOURCE/core.py" "$DEST/core.py"
install -m 0755 "$SOURCE/predator-fan-helper" "$DEST/predator-fan-helper"
install -m 0755 "$SOURCE/predator-fan-manager" "$DEST/predator-fan-manager"
install -m 0755 "$SOURCE/predator-probe" "$DEST/predator-probe"

cat > /usr/local/bin/predator-fan-manager <<EOF
#!/bin/sh
exec /usr/bin/python3 "$DEST/predator-fan-manager" "\$@"
EOF
cat > /usr/local/bin/predator-probe <<EOF
#!/bin/sh
exec /usr/bin/python3 "$DEST/predator-probe" "\$@"
EOF
chmod 0755 /usr/local/bin/predator-fan-manager /usr/local/bin/predator-probe

sed "s/@UID@/$TARGET_UID/g" "$SOURCE/predator-fan-helper.service" > "$UNIT"
systemctl daemon-reload
systemctl enable predator-fan-helper.service >/dev/null
systemctl restart predator-fan-helper.service

install -d -m 0755 /usr/share/applications
install -m 0644 "$SOURCE/predator-fan-manager.desktop" \
    /usr/share/applications/predator-fan-manager.desktop

install -d -m 0755 -o "$TARGET_USER" -g "$TARGET_GID" "$TARGET_HOME/.config"
install -d -m 0755 -o "$TARGET_USER" -g "$TARGET_GID" "$TARGET_HOME/.config/autostart"
sed 's/ --show//' "$SOURCE/predator-fan-manager.desktop" > \
    "$TARGET_HOME/.config/autostart/predator-fan-manager.desktop"
chown "$TARGET_USER:$TARGET_GID" "$TARGET_HOME/.config/autostart/predator-fan-manager.desktop"
chmod 0644 "$TARGET_HOME/.config/autostart/predator-fan-manager.desktop"

echo "Installed Predator Fan Manager."
echo "Run the read-only probe first: sudo predator-probe"
echo "Open the app: predator-fan-manager --show"
echo "Helper logs: journalctl -u predator-fan-helper -f"
