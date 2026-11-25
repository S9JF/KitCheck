# ===================== KitCheck (cloud GPU VM): real-time detection results into CiRA =====================
# gpu_station.py on this VM checks EVERY camera frame with YOLO11n on the T4 (10 frames/s) and sends each result
# to KitCheck itself. GstCam shows that video with the boxes drawn in (http://172.17.0.1:8083/stream).
# This node (CiRA runs it once per frame in a fresh python3.12, with payload and img) only copies the newest
# result into the payload, so later nodes / Debug can use it:
#   payload["detections"] = [{"type","conf","qr","box":[x,y,w,h] (0..1)}], payload["kitcheck"] = status
import json
import time
import urllib.request
LATEST = 'http://172.17.0.1:8090/latest'
STATUS = {'last_error': ''}
try:
	with urllib.request.urlopen(LATEST, timeout=2) as r:
		res = json.load(r)
	payload.update({k: res[k] for k in ('detections', 'duration', 'ts', 'image_w', 'image_h') if k in res})
	STATUS['scan_waiting'] = bool(res.get('wanted'))
	STATUS['age_s'] = round(time.time() - res.get('ts', 0), 2)
	STATUS['tools'] = [d['type'] for d in res.get('detections', [])]
except Exception as e:
	STATUS['last_error'] = repr(e)[:300]
payload['kitcheck'] = STATUS
time.sleep(0.2)  # the flow's loop waits for this script: about 3-4 updates a second is plenty for Debug
# ==========================================================================================================
