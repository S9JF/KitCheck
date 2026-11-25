#!/bin/bash
# MQTT broker on this server (mosquitto) + public TLS entry through Tailscale Funnel.
#   sudo bash ~/kitcheck_v2/mqtt-setup.sh
#
#   LAN / this machine :  192.168.1.189:1883  (plain MQTT, password required)
#   anywhere           :  <this-host>.ts.net:8443  (MQTT over TLS via Tailscale Funnel)
#
# Users:  kitcheck  - the KitCheck service (random password, written to /etc/kitcheck.env)
#         tray      - the ESP32 scale (you choose the password; it may only publish kitcheck/scale)
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }

apt-get update -q
DEBIAN_FRONTEND=noninteractive apt-get install -y -q mosquitto mosquitto-clients

cat > /etc/mosquitto/conf.d/kitcheck.conf <<'EOF'
listener 1883
allow_anonymous false
password_file /etc/mosquitto/passwd
acl_file /etc/mosquitto/kitcheck.acl
EOF

cat > /etc/mosquitto/kitcheck.acl <<'EOF'
user kitcheck
topic readwrite kitcheck/#

user tray
topic write kitcheck/scale

user cira
topic readwrite kitcheck/#
EOF

touch /etc/mosquitto/passwd
SVC_PASS=$(openssl rand -hex 24)
mosquitto_passwd -b /etc/mosquitto/passwd kitcheck "$SVC_PASS"
echo
echo "Choose a password for the ESP32 (user: tray). You will put the same one in secrets.h."
mosquitto_passwd /etc/mosquitto/passwd tray
chown root:mosquitto /etc/mosquitto/passwd /etc/mosquitto/kitcheck.acl
chmod 640 /etc/mosquitto/passwd /etc/mosquitto/kitcheck.acl

systemctl enable mosquitto
systemctl restart mosquitto
sleep 1
systemctl is-active mosquitto

# KitCheck: connect to the local broker (camera detections stay simulated until CiRA is wired up)
ENV=/etc/kitcheck.env
cp -a "$ENV" "$ENV.bak-$(date +%Y%m%d-%H%M%S)"
sed -i '/^KITCHECK_MQTT_\(ENABLE\|HOST\|PORT\|USER\|PASS\|PREFIX\)=/d' "$ENV"
{
  echo "KITCHECK_MQTT_ENABLE=1"
  echo "KITCHECK_MQTT_HOST=127.0.0.1"
  echo "KITCHECK_MQTT_PORT=1883"
  echo "KITCHECK_MQTT_PREFIX=kitcheck"
  echo "KITCHECK_MQTT_USER=kitcheck"
  echo "KITCHECK_MQTT_PASS=$SVC_PASS"
} >> "$ENV"
unset SVC_PASS
systemctl restart kitcheck
sleep 3
curl -s http://127.0.0.1:8080/api/health; echo

# Public TLS entry: <host>.ts.net:8443 -> 127.0.0.1:1883
echo
if tailscale funnel --bg --tls-terminated-tcp=8443 tcp://127.0.0.1:1883; then
  tailscale funnel status
else
  echo "Funnel is not enabled for this machine yet."
  echo "Open the link above (or login.tailscale.com -> Access controls), allow Funnel and HTTPS, then run:"
  echo "  sudo tailscale funnel --bg --tls-terminated-tcp=8443 tcp://127.0.0.1:1883"
fi
