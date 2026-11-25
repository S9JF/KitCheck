#!/bin/bash
# Install KitCheck v2 (self-service kiosk, per-item loans) over /opt/kitcheck.
#   sudo bash ~/kitcheck_v2/deploy.sh
# Backs up the current code first; the database only gains new tables (old data is kept).
set -euo pipefail
SRC="$(cd "$(dirname "$0")" && pwd)"
TS=$(date +%Y%m%d-%H%M%S)
BAK="/opt/kitcheck/kitcheck.bak-$TS"

[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
[ -f "$SRC/kitcheck/desk.py" ] || { echo "kitcheck/desk.py not found next to this script"; exit 1; }

cp -a /opt/kitcheck/kitcheck "$BAK"
# Pillow: /api/camera/small shrinks camera frames for the ESP32 kiosk screen (optional, falls back to full size)
/opt/kitcheck/venv/bin/pip install -q "pillow>=10" || echo "warning: pillow not installed, kiosk camera stays full size"
cp -r "$SRC/kitcheck/." /opt/kitcheck/kitcheck/
find /opt/kitcheck/kitcheck -name __pycache__ -prune -exec rm -rf {} +
chown -R root:root /opt/kitcheck/kitcheck
chmod -R u=rwX,go=rX /opt/kitcheck/kitcheck

systemctl restart kitcheck
sleep 3
systemctl is-active kitcheck
curl -s http://127.0.0.1:8080/api/health; echo
echo
echo "installed. backup of old code: $BAK"
echo "rollback: sudo rm -rf /opt/kitcheck/kitcheck && sudo mv $BAK /opt/kitcheck/kitcheck && sudo systemctl restart kitcheck"
