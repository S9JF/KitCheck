#!/bin/bash
# Export CiRA CORE (image ciracore-local:1.9.4 + the ciracore-home volume with your projects) so the Google Cloud
# GPU VM can load it, then serve the files ONLY over Tailscale until the VM has downloaded them.
#   sudo bash ~/kitcheck_v2/cira-export.sh
# Nothing secret is printed. The download link has a random folder name and stops when you press Ctrl+C
# (or after 2 hours).
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
OUT=/home/sear/cira-export
TS_IP=$(tailscale ip -4 | head -1)
mkdir -p "$OUT"
DIR="$OUT/$(head -c 12 /dev/urandom | od -An -tx1 | tr -d ' \n')"
mkdir -p "$DIR"

echo "[1/3] image ciracore-local:1.9.4 -> $DIR/ciracore-image.tar.zst"
docker image inspect ciracore-local:1.9.4 --format 'size: {{.Size}} bytes'
docker save ciracore-local:1.9.4 | zstd -T0 -3 -q -o "$DIR/ciracore-image.tar.zst"

echo "[2/3] volume ciracore-home (CiRA projects/settings) -> ciracore-home.tar.zst"
docker run --rm --entrypoint tar -v ciracore-home:/v:ro ciracore-local:1.9.4 -C /v -cf - . | zstd -T0 -3 -q -o "$DIR/ciracore-home.tar.zst"
( cd "$DIR" && sha256sum *.zst > SHA256SUMS )
chown -R sear:sear "$OUT"
ls -la "$DIR"

echo "[3/3] serving on http://$TS_IP:8099/$(basename "$DIR")/  (Tailscale only; Ctrl+C when Claude says done)"
echo "LINK: http://$TS_IP:8099/$(basename "$DIR")/"
cd "$OUT"
timeout 7200 python3 -m http.server 8099 --bind "$TS_IP" || true
echo "stopped serving. Remove the export with:  sudo rm -rf $OUT"
