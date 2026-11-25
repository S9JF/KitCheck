#!/bin/bash
# Make sure CiRA CORE's Python (inside the container) can run our YOLO11n NCNN model.
#   sudo bash ~/kitcheck_v2/cira-python-deps.sh
# Installs into the cira user's site-packages only (pip --user); CiRA's own OpenCV stays in charge:
# ultralytics is installed without its opencv-python dependency so it cannot shadow CiRA's cv2.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
PY="docker exec -u cira ciracore python3"
echo "--- before"
$PY -c 'import cv2; print("cv2", cv2.__version__, cv2.__file__)'
$PY -c 'import ultralytics; print("ultralytics", ultralytics.__version__)' 2>/dev/null || echo "ultralytics: missing"
$PY -c 'import torch; print("torch", torch.__version__)' 2>/dev/null || echo "torch: missing"
$PY -c 'import ncnn; print("ncnn ok")' 2>/dev/null || echo "ncnn: missing"

PIP="docker exec -u cira ciracore python3 -m pip install --user --break-system-packages -q"
$PY -c 'import torch' 2>/dev/null || $PIP torch torchvision --index-url https://download.pytorch.org/whl/cpu
$PY -c 'import ultralytics' 2>/dev/null || { $PIP --no-deps ultralytics ultralytics-thop; $PIP matplotlib pillow pyyaml requests scipy tqdm psutil polars; }
$PY -c 'import ncnn' 2>/dev/null || $PIP ncnn

echo "--- after"
$PY -c 'import cv2; print("cv2", cv2.__version__, cv2.__file__)'
$PY -c 'import ultralytics, torch, ncnn; print("ultralytics", ultralytics.__version__, "| torch", torch.__version__, "| ncnn ok")'
$PY - <<'PY'
from ultralytics import YOLO
import numpy as np
m = YOLO("/home/cira/Models/cira_tools_yolo11n/last_ncnn_model", task="detect")
m(np.zeros((640, 640, 3), np.uint8), verbose=False)
print("model loads and runs inside CiRA: OK")
PY
