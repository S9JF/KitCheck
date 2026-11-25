#!/bin/bash
# Cloudflare Tunnel for kitcheck.saer.lol -> http://localhost:8080
#   sudo bash cf-setup.sh install   # add Cloudflare's apt repo, install cloudflared
#   (as sear)  cloudflared tunnel login / create / route dns   -> ~/.cloudflared
#   sudo bash cf-setup.sh service   # copy config + credentials to /etc/cloudflared, run as a service
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
SEARHOME=/home/sear

case "${1:-}" in
install)
  mkdir -p --mode=0755 /usr/share/keyrings
  curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg -o /usr/share/keyrings/cloudflare-main.gpg
  echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main" \
    > /etc/apt/sources.list.d/cloudflared.list
  apt-get update -q
  DEBIAN_FRONTEND=noninteractive apt-get install -y -q cloudflared
  cloudflared --version
  ;;
service)
  CFG="$SEARHOME/.cloudflared/config.yml"
  [ -f "$CFG" ] || { echo "missing $CFG"; exit 1; }
  CRED=$(awk '/^credentials-file:/{print $2}' "$CFG")
  [ -f "$CRED" ] || { echo "missing credentials $CRED"; exit 1; }
  mkdir -p /etc/cloudflared
  install -m 600 "$CRED" "/etc/cloudflared/$(basename "$CRED")"
  sed "s#^credentials-file:.*#credentials-file: /etc/cloudflared/$(basename "$CRED")#" "$CFG" > /etc/cloudflared/config.yml
  chmod 644 /etc/cloudflared/config.yml
  if systemctl list-unit-files | grep -q '^cloudflared.service'; then
    systemctl restart cloudflared
  else
    cloudflared --config /etc/cloudflared/config.yml service install
  fi
  systemctl enable cloudflared >/dev/null 2>&1 || true
  sleep 3
  systemctl is-active cloudflared
  ;;
*)
  echo "usage: sudo bash $0 install|service"; exit 1 ;;
esac
