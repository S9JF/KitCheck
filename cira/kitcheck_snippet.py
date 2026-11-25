# ===================== KitCheck: YOLO11n + QR (ncnn only, no torch) =====================
# Input : camera image (GstCam / Image File).
# Output: payload {"detections":[{"type","conf","qr","box":[x,y,w,h]}], "kitcheck":{status}}  (box = 0..1)
#         + the image with boxes drawn, for the CiRA viewer.
# Runs the NCNN export of our model with the light `ncnn` package directly (no ultralytics/torch, which
# can crash inside CiRA's ROS process). The model is loaded on the first image, and every problem is
# reported in payload["kitcheck"] so the Debug node shows it.
import time
import traceback
MODEL_DIR = '/home/cira/Models/cira_tools_yolo11n'
CONF = 0.45          # minimum confidence
IOU = 0.5            # NMS overlap
MIN_INTERVAL = 0.3   # seconds between two inferences (the CPU is small)
SIZE = 640

STATUS = {'model': 'not loaded yet', 'model_dir': MODEL_DIR, 'last_error': ''}
net = None
names = []
qr_reader = cv2.QRCodeDetector()
last_t = 0.0
last_out = {'detections': []}

def load_model():
    global net, names
    import ncnn
    d = MODEL_DIR + '/last_ncnn_model'
    n = ncnn.Net()
    n.opt.use_vulkan_compute = False
    n.opt.num_threads = 2
    n.load_param(d + '/model.ncnn.param')
    n.load_model(d + '/model.ncnn.bin')
    with open(MODEL_DIR + '/obj.names', encoding='utf-8') as f:
        names = [l.strip() for l in f if l.strip()]
    net = n
    STATUS['model'] = 'loaded (%d classes)' % len(names)

def yolo(img):
    import ncnn
    h, w = img.shape[:2]
    r = min(SIZE / h, SIZE / w)
    nh, nw = int(round(h * r)), int(round(w * r))
    top, left = (SIZE - nh) // 2, (SIZE - nw) // 2
    canvas = np.full((SIZE, SIZE, 3), 114, np.uint8)
    canvas[top:top + nh, left:left + nw] = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_LINEAR)
    mat = ncnn.Mat.from_pixels(canvas, ncnn.Mat.PixelType.PIXEL_BGR2RGB, SIZE, SIZE)
    mat.substract_mean_normalize([], [1 / 255.0, 1 / 255.0, 1 / 255.0])
    ex = net.create_extractor()
    ex.input('in0', mat)
    _, out = ex.extract('out0')
    a = np.array(out).reshape(4 + len(names), -1)      # rows: cx, cy, w, h, class scores
    scores = a[4:]
    cls = scores.argmax(0)
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
        x, y = max(0.0, x), max(0.0, y)
        ww, hh = min(ww, w - x), min(hh, h - y)
        res.append((names[int(cls[i])], float(conf[i]), x, y, x + ww, y + hh))
    return res

def read_qrs(img):
    """[(text, cx, cy)] for every QR code found in the image."""
    out = []
    ok, texts, points, _ = qr_reader.detectAndDecodeMulti(img)
    if ok and points is not None:
        for t, p in zip(texts, points):
            if t:
                out.append((t.strip(), float(p[:, 0].mean()), float(p[:, 1].mean())))
    return out

def kitcheck_detect(img):
    h, w = img.shape[:2]
    t0 = time.time()
    if net is None:
        load_model()
    dets = [{'type': n, 'conf': round(c, 3), 'qr': None,
             'box': [round(x1 / w, 4), round(y1 / h, 4), round((x2 - x1) / w, 4), round((y2 - y1) / h, 4)],
             '_px': (x1, y1, x2, y2)} for n, c, x1, y1, x2, y2 in yolo(img)]
    # give each QR to the smallest detection box that contains its centre
    for text, cx, cy in read_qrs(img):
        inside = [d for d in dets if d['qr'] is None and d['_px'][0] <= cx <= d['_px'][2] and d['_px'][1] <= cy <= d['_px'][3]]
        if inside:
            min(inside, key=lambda d: (d['_px'][2] - d['_px'][0]) * (d['_px'][3] - d['_px'][1]))['qr'] = text
    for d in dets:   # draw for the CiRA viewer
        x1, y1, x2, y2 = [int(v) for v in d.pop('_px')]
        col = (34, 160, 90) if d['qr'] else (31, 154, 227)
        cv2.rectangle(img, (x1, y1), (x2, y2), col, 3)
        cv2.putText(img, '%s %.2f %s' % (d['type'], d['conf'], d['qr'] or 'QR?'), (x1, max(18, y1 - 6)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, col, 2)
    return {'detections': dets, 'duration': round(time.time() - t0, 3), 'ts': round(time.time(), 3),
            'image_w': w, 'image_h': h}

def handle_request(req):
	# Everything (status, errors with traceback) goes into the payload, so the Debug node shows it.
	global last_t, last_out
	payload, img = {}, None
	try:
		payload, img = getReqData(req)
		STATUS['got_image'] = img is not None
		if img is not None:
			STATUS['image_shape'] = list(img.shape)
		if img is not None and time.time() - last_t >= MIN_INTERVAL:
			last_t = time.time()
			if img.ndim == 2:
				img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
			elif img.shape[2] == 4:
				img = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
			img = np.ascontiguousarray(img)
			last_out = kitcheck_detect(img)
		STATUS['last_error'] = ''
	except Exception:
		STATUS['last_error'] = traceback.format_exc()[-1500:]
		print('KitCheck error:', STATUS['last_error'])
	try:
		payload.update(last_out)
		payload['kitcheck'] = STATUS
		return makeRespData(payload, img)
	except Exception:
		payload = {'kitcheck': dict(STATUS, last_error=traceback.format_exc()[-1500:])}
		return makeRespData(payload, None)
# =========================================================================================
