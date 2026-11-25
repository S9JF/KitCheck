#!/bin/bash
# Undo NumPy 2 in the cira user's site-packages: CiRA's OpenCV (cv2 4.12 in /usr/local) is built
# for NumPy 1.x. Pin numpy<2 for the cira user, then check cv2, ros_numpy, ultralytics and our model.
#   sudo bash ~/kitcheck_v2/cira-python-fix.sh
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
PY="docker exec -u cira ciracore python3"
PIP="docker exec -u cira ciracore python3 -m pip"

echo "--- system numpy (CiRA's own):"
docker exec -u cira ciracore python3 -s -c 'import numpy; print(numpy.__version__, numpy.__file__)' || true
$PIP install --user --break-system-packages -q "numpy<2" "scipy<1.15" "matplotlib<3.10" "contourpy<1.4"

echo "--- check"
$PY -c 'import numpy; print("numpy", numpy.__version__)'
$PY -c 'import cv2; print("cv2", cv2.__version__)'
$PY -c 'import sys; sys.path.append("/home/cira/.cira_core_install/cira_libs_ws/install/lib/python3/dist-packages"); import ros_numpy; print("ros_numpy ok")' || echo "ros_numpy import check skipped/failed (see above)"
$PY -c 'import ultralytics, torch, ncnn; print("ultralytics", ultralytics.__version__, "| torch", torch.__version__, "| ncnn ok")'
$PY - <<'PY'
from ultralytics import YOLO
import numpy as np, cv2
m = YOLO("/home/cira/Models/cira_tools_yolo11n/last_ncnn_model", task="detect")
m(np.zeros((640, 640, 3), np.uint8), verbose=False)
ok, *_ = cv2.QRCodeDetector().detectAndDecodeMulti(np.zeros((100, 100, 3), np.uint8))
print("model + QR run inside CiRA: OK")
PY
