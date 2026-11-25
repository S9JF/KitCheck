#!/bin/bash
# Raise the USB camera (ustreamer, sear-camera.service) to 30 fps when the camera offers it at 1280x720 MJPEG.
#   sudo bash ~/kitcheck_v2/cam-fps.sh        (or: sudo bash ~/kitcheck_v2/cam-fps.sh 25)
# The unit's command line holds the camera password, so it is edited in place and never printed.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
FPS="${1:-30}"
DEV=/dev/v4l/by-id/usb-Sonix_Technology_Co.__Ltd._USB_2.0_Camera_SN0001-video-index0
UNIT=$(systemctl show -p FragmentPath --value sear-camera)

echo "camera modes at 1280x720 (MJPG):"
v4l2-ctl -d "$DEV" --list-formats-ext 2>/dev/null | awk '
  /\[[0-9]+\]:/ { mj = ($0 ~ /MJPG/) }
  mj && /Size:/ { on = ($0 ~ /1280x720/) }
  mj && on && /Interval/ { print "   " $0 }' || true
echo "(ustreamer will use the closest rate the camera offers)"

cp -a "$UNIT" "$UNIT.bak-$(date +%Y%m%d-%H%M%S)"
sed -i "s/--desired-fps=[0-9]*/--desired-fps=$FPS/" "$UNIT"
grep -o -- '--desired-fps=[0-9]*' "$UNIT"
systemctl daemon-reload
systemctl restart sear-camera
sleep 3
systemctl is-active sear-camera
journalctl -u sear-camera --since '-10 s' --no-pager | grep -iE 'fps|resolution|error' | grep -viE 'passw' | tail -5 || true
