#!/bin/bash
# Prepare CiRA CORE for KitCheck:
#  1. copy the YOLO11n model (NCNN, as CiRA's DeepDetect expects) into the container: /home/cira/Models/cira_tools_yolo11n
#  2. MQTT for the container: extra mosquitto listener on the Docker bridge only (172.17.0.1:1884),
#     no password, may only use kitcheck/#. The password-protected listener on 1883 stays as it is.
#   sudo bash ~/kitcheck_v2/cira-setup.sh
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }

MODEL=/home/sear/yolo_model/cira_tools_yolo11n
[ -f "$MODEL/last_ncnn_model/model.ncnn.bin" ] || { echo "model not found in $MODEL"; exit 1; }
docker exec ciracore mkdir -p /home/cira/Models
docker cp "$MODEL" ciracore:/home/cira/Models/
CIRA_UID=$(docker exec ciracore id -u cira 2>/dev/null || echo 1000)
docker exec -u 0 ciracore chown -R "$CIRA_UID:$CIRA_UID" /home/cira/Models 2>/dev/null || true
docker exec ciracore ls -la /home/cira/Models/cira_tools_yolo11n /home/cira/Models/cira_tools_yolo11n/last_ncnn_model

cp -a /etc/mosquitto/conf.d/kitcheck.conf "/etc/mosquitto/conf.d/kitcheck.conf.bak-$(date +%Y%m%d-%H%M%S)"
cat > /etc/mosquitto/conf.d/kitcheck.conf <<'EOF'
per_listener_settings true

# LAN + Tailscale Funnel: password required
listener 1883
allow_anonymous false
password_file /etc/mosquitto/passwd
acl_file /etc/mosquitto/kitcheck.acl

# CiRA CORE container only (Docker bridge address, not reachable from outside this machine)
listener 1884 172.17.0.1
allow_anonymous true
acl_file /etc/mosquitto/cira.acl
EOF
cat > /etc/mosquitto/cira.acl <<'EOF'
# anonymous clients on the Docker listener
topic readwrite kitcheck/#
EOF
chown root:mosquitto /etc/mosquitto/cira.acl
chmod 640 /etc/mosquitto/cira.acl
systemctl restart mosquitto
sleep 1
systemctl is-active mosquitto
ss -ltn | grep -E ':(1883|1884) '
echo "done: model in /home/cira/Models/cira_tools_yolo11n, MQTT for CiRA at 172.17.0.1:1884"
