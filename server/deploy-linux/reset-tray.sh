#!/bin/bash
# Set the MQTT password of user "tray" from ~/kitcheck_v2/.tray_pw, then delete that file.
#   sudo bash ~/kitcheck_v2/reset-tray.sh
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
PWF="$(cd "$(dirname "$0")" && pwd)/.tray_pw"
[ -s "$PWF" ] || { echo "no $PWF"; exit 1; }
mosquitto_passwd -b /etc/mosquitto/passwd tray "$(tr -d '\r\n' < "$PWF")"
chown root:mosquitto /etc/mosquitto/passwd
chmod 640 /etc/mosquitto/passwd
shred -u "$PWF"
systemctl reload mosquitto 2>/dev/null || systemctl restart mosquitto
echo "tray password updated (same value as in the ESP32 secrets.h)"
