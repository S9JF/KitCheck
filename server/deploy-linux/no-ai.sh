#!/bin/bash
# Turn the AI (CiRA CORE) off: KitCheck goes back to simulated detections, the tray scale keeps working over
# MQTT, and the CiRA container stops so its CPU is free for the camera views.
#   sudo bash ~/kitcheck_v2/no-ai.sh
# Undo: set KITCHECK_MODE=mqtt in /etc/kitcheck.env, `sudo systemctl restart kitcheck`, `sudo docker start ciracore`.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
ENV=/etc/kitcheck.env

set_var() {  # set NAME=VALUE in $ENV without printing the file (it holds secrets)
  if grep -q "^$1=" "$ENV"; then sed -i "s|^$1=.*|$1=$2|" "$ENV"; else echo "$1=$2" >> "$ENV"; fi
  echo "set $1=$2"
}
cp -a "$ENV" "$ENV.bak-$(date +%Y%m%d-%H%M%S)"
set_var KITCHECK_MODE sim
set_var KITCHECK_MQTT_ENABLE 1   # still needed for the tray weight

systemctl restart kitcheck
sleep 3
curl -s http://127.0.0.1:8080/api/health; echo

if docker ps --format '{{.Names}}' | grep -qx ciracore; then
  docker stop ciracore >/dev/null && echo "stopped CiRA CORE container (start again: sudo docker start ciracore)"
fi
uptime
