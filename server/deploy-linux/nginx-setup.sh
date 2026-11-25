#!/bin/bash
# nginx reverse proxy for KitCheck at kitcheck.saer.lol
#   sudo bash ~/kitcheck_v2/nginx-setup.sh          # step 1: install nginx, serve http
#   sudo bash ~/kitcheck_v2/nginx-setup.sh --tls    # step 2: Let's Encrypt certificate + redirect to https
# Step 2 needs the DNS record and the router port-forward (80, 443 -> this server) to be in place.
set -euo pipefail
DOMAIN=kitcheck.saer.lol
UPSTREAM=127.0.0.1:8080
SITE=/etc/nginx/sites-available/kitcheck

[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }

if [ "${1:-}" = "--tls" ]; then
  certbot --nginx -d "$DOMAIN" --redirect
  nginx -t && systemctl reload nginx
  echo "done: https://$DOMAIN"
  exit 0
fi

apt-get update -q
DEBIAN_FRONTEND=noninteractive apt-get install -y -q nginx certbot python3-certbot-nginx

cat > "$SITE" <<EOF
server {
    listen 80;
    listen [::]:80;
    server_name $DOMAIN;

    client_max_body_size 16m;          # station uploads tray images
    server_tokens off;

    location / {
        proxy_pass http://$UPSTREAM;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 30s;        # a kiosk scan waits up to 15 s for the camera
    }
}
EOF
ln -sf "$SITE" /etc/nginx/sites-enabled/kitcheck
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl enable --now nginx
systemctl reload nginx
curl -s -o /dev/null -w "local check via nginx: %{http_code}\n" -H "Host: $DOMAIN" http://127.0.0.1/kiosk
echo "step 1 done. Next: DNS A record + router port-forward, then: sudo bash $0 --tls"
