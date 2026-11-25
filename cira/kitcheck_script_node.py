# ===================== KitCheck: YOLO11n + QR for CiRA's "Python" (PythonScript) node =====================
# CiRA runs this whole script once per frame in a fresh python3.12, with two variables ready:
#   payload (dict) and img (BGR numpy image; 1x1 black when there is no new image).
# Whatever payload / img hold at the end is passed on to the next node, so we just modify them.
# Adds payload["detections"] = [{"type","conf","qr","box":[x,y,w,h] (0..1)}] and payload["kitcheck"] = status,
# Boxes are rendered by the browser. Keep img free of annotations for the review image.
# Errors go into payload["kitcheck"]["last_error"] (see it in Debug).
#
# Event-only: YOLO runs only while KitCheck has a borrow/return scan waiting (asked at WANTED_URL, a cheap
# stdlib request before cv2/numpy/ncnn are even imported). Otherwise the script sleeps IDLE_S and passes the
# frame on untouched, so the server's CPU is free for the camera views.
import json
import time
import traceback
import urllib.request
WANTED_URL = 'http://172.17.0.1:8080/api/station/wanted'
IDLE_S = 1.0         # the flow's loop waits for this script, so this sets the idle frame rate
MODEL_DIR = '/home/cira/Models/cira_tools_yolo11n'
CONF = 0.45          # minimum confidence
IOU = 0.5            # NMS overlap
# The same YOLO11n exported (no retraining) at 416 px input: 2.5x faster than 640 on the server, same results on
# the test images (export_bench.py). Falls back to the 640 export when the 416 one is not in the container.
NCNN_SIZES = [(416, '/last_ncnn_model_416'), (640, '/last_ncnn_model')]
SIZE = 640
# Only the tool types KitCheck lends (desk.YOLO_ALIASES); the model's other 22 classes (try_square, chainsaw…)
# are ignored, so they can no longer win over a real tool or show up as unknown items.
KEEP = ('hammer', 'screwdriver', 'pliers', 'wrench', 'drill')
STATUS = {'model': 'not loaded', 'last_error': ''}


def load_model():
	import os
	import ncnn
	global SIZE
	for size, sub in NCNN_SIZES:
		d = MODEL_DIR + sub
		if os.path.exists(d + '/model.ncnn.bin'):
			SIZE = size
			break
	n = ncnn.Net()
	n.opt.use_vulkan_compute = False
	n.opt.num_threads = 2
	n.load_param(d + '/model.ncnn.param')
	n.load_model(d + '/model.ncnn.bin')
	with open(MODEL_DIR + '/obj.names', encoding='utf-8') as f:
		names = [l.strip() for l in f if l.strip()]
	STATUS['model'] = 'loaded %d px, %d of %d classes' % (SIZE, sum(x in KEEP for x in names), len(names))
	return n, names


def yolo(net, names, im):
	import ncnn
	h, w = im.shape[:2]
	r = min(SIZE / h, SIZE / w)
	nh, nw = int(round(h * r)), int(round(w * r))
	top, left = (SIZE - nh) // 2, (SIZE - nw) // 2
	canvas = np.full((SIZE, SIZE, 3), 114, np.uint8)
	canvas[top:top + nh, left:left + nw] = cv2.resize(im, (nw, nh), interpolation=cv2.INTER_LINEAR)
	mat = ncnn.Mat.from_pixels(canvas, ncnn.Mat.PixelType.PIXEL_BGR2RGB, SIZE, SIZE)
	mat.substract_mean_normalize([], [1 / 255.0, 1 / 255.0, 1 / 255.0])
	ex = net.create_extractor()
	ex.input('in0', mat)
	_, out = ex.extract('out0')
	a = np.array(out).reshape(4 + len(names), -1)      # rows: cx, cy, w, h, class scores
	kept = np.array([i for i, n in enumerate(names) if n in KEEP] or list(range(len(names))))
	scores = a[4:][kept]                                # only the tools KitCheck lends compete
	cls = kept[scores.argmax(0)]
	conf = scores.max(0)
	keep = conf >= CONF
	if not keep.any():
		return []
	cx, cy, bw, bh = a[0][keep], a[1][keep], a[2][keep], a[3][keep]
	cls, conf = cls[keep], conf[keep]
	x1 = (cx - bw / 2 - left) / r
	y1 = (cy - bh / 2 - top) / r
	rects = [[float(x), float(y), float(ww / r), float(hh / r)] for x, y, ww, hh in zip(x1, y1, bw, bh)]
	idx = cv2.dnn.NMSBoxesBatched(rects, conf.tolist(), cls.tolist(), CONF, IOU)
	res = []
	for i in np.array(idx).flatten():
		x, y, ww, hh = rects[i]
		x2, y2 = min(float(w), x + ww), min(float(h), y + hh)
		x1, y1 = max(0.0, x), max(0.0, y)
		if x2 > x1 and y2 > y1:
			res.append((names[int(cls[i])], float(conf[i]), x1, y1, x2, y2))
	return res


def read_qrs(im):
	out = []
	ok, texts, points, _ = cv2.QRCodeDetector().detectAndDecodeMulti(im)
	if ok and points is not None:
		for t, p in zip(texts, points):
			if t:
				out.append((t.strip(), float(p[:, 0].mean()), float(p[:, 1].mean())))
	return out


def kitcheck_detect(im):
	h, w = im.shape[:2]
	t0 = time.time()
	net, names = load_model()
	dets = [{'type': n, 'conf': round(c, 3), 'qr': None,
			 'box': [round(x1 / w, 4), round(y1 / h, 4), round((x2 - x1) / w, 4), round((y2 - y1) / h, 4)],
			 '_px': (x1, y1, x2, y2)} for n, c, x1, y1, x2, y2 in yolo(net, names, im)]
	for text, cx, cy in read_qrs(im):    # each QR goes to the smallest box containing its centre
		inside = [d for d in dets if d['qr'] is None and d['_px'][0] <= cx <= d['_px'][2] and d['_px'][1] <= cy <= d['_px'][3]]
		if inside:
			min(inside, key=lambda d: (d['_px'][2] - d['_px'][0]) * (d['_px'][3] - d['_px'][1]))['qr'] = text
	for d in dets:
		d.pop('_px')
	return {'detections': dets, 'duration': round(time.time() - t0, 3), 'ts': round(time.time(), 3),
			'image_w': w, 'image_h': h}


def scan_wanted():
	try:
		with urllib.request.urlopen(WANTED_URL, timeout=1) as r:
			return bool(json.load(r).get('wanted'))
	except Exception as e:
		STATUS['wanted_error'] = repr(e)[:200]
		return True  # KitCheck unreachable: keep detecting so scans still work


try:
	if not scan_wanted():
		STATUS['idle'] = True
		time.sleep(IDLE_S)
	elif img is None or img.shape[0] <= 1 or img.shape[1] <= 1:
		STATUS['got_image'] = False
	else:
		import cv2
		import numpy as np
		STATUS['got_image'] = True
		if img.ndim == 2:
			img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
		elif img.shape[2] == 4:
			img = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
		img = np.ascontiguousarray(img)
		payload.update(kitcheck_detect(img))
except Exception:
	STATUS['last_error'] = traceback.format_exc()[-1500:]
payload['kitcheck'] = STATUS
# ==========================================================================================================
