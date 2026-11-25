"""KitCheck AI worker for a GPU machine (YOLO11n on CUDA + QR), replacing CiRA's per-frame Python node.

The model is loaded once and stays on the GPU. While KitCheck has a borrow/return scan waiting (GET /wanted),
the worker keeps the camera's MJPEG stream open, runs YOLO on the newest frame and POSTs each result to
KitCheck's /api/station/live (the same JSON CiRA sent: detections with 0..1 boxes, type, conf, qr). When no
scan has been waiting for KEEP_S, the stream is closed so the camera link (home upload) is idle.

Settings (environment, e.g. /etc/kitcheck-ai.env):
    KITCHECK_URL      http://100.101.113.108:8080      KitCheck (over Tailscale)
    CAMERA_URL        http://100.101.113.108:8081      ipcam.py / ustreamer (GET /stream)
    CAMERA_USER       camera
    CAMERA_PASS       (required)
    MODEL             ~/model/last.pt
    IMGSZ 640  CONF 0.45  IOU 0.5  KEEP_S 30
"""
import base64
import http.client
import json
import os
import threading
import time
import urllib.parse
import urllib.request

import cv2
import numpy as np

KITCHECK = os.environ.get("KITCHECK_URL", "http://100.101.113.108:8080").rstrip("/")
CAM = urllib.parse.urlsplit(os.environ.get("CAMERA_URL", "http://100.101.113.108:8081"))
CAM_AUTH = "Basic " + base64.b64encode(("%s:%s" % (os.environ.get("CAMERA_USER", "camera"),
                                                     os.environ.get("CAMERA_PASS", ""))).encode()).decode()
MODEL = os.path.expanduser(os.environ.get("MODEL", "~/model/last.pt"))
IMGSZ = int(os.environ.get("IMGSZ", "640"))
CONF = float(os.environ.get("CONF", "0.45"))
IOU = float(os.environ.get("IOU", "0.5"))
KEEP_S = float(os.environ.get("KEEP_S", "30"))
# Only the tool types KitCheck lends (desk.YOLO_ALIASES); the model's other classes are ignored.
KEEP = ("hammer", "screwdriver", "pliers", "wrench", "drill")


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


class Wanted:
    """Polls KitCheck's /api/station/wanted; last_true = when a scan was last waiting."""

    def __init__(self):
        self.last_true = 0.0
        self.now = False
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while True:
            try:
                with urllib.request.urlopen(KITCHECK + "/api/station/wanted", timeout=3) as r:
                    self.now = bool(json.load(r).get("wanted"))
            except Exception as e:  # KitCheck unreachable: assume nothing waits
                self.now = False
                log("wanted?", repr(e)[:120])
                time.sleep(2)
            if self.now:
                self.last_true = time.time()
            time.sleep(0.2)


class Stream:
    """Reads the camera's MJPEG stream in a thread and keeps only the newest JPEG."""

    def __init__(self):
        self.jpeg, self.seq, self.stop = None, 0, False
        self.cond = threading.Condition()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        conn = http.client.HTTPConnection(CAM.hostname, CAM.port or 80, timeout=10)
        try:
            conn.request("GET", CAM.path.rstrip("/") + "/stream", headers={"Authorization": CAM_AUTH})
            r = conn.getresponse()
            if r.status != 200:
                log("camera answered", r.status)
                return
            while not self.stop:
                line = r.readline(512)
                if not line:
                    return
                if not line.lower().startswith(b"content-length"):
                    continue
                n = int(line.split(b":")[1])
                while r.readline(512).strip():
                    pass
                data = r.read(n)
                with self.cond:
                    self.jpeg, self.seq = data, self.seq + 1
                    self.cond.notify_all()
        except Exception as e:
            log("camera stream ended:", repr(e)[:120])
        finally:
            conn.close()
            with self.cond:
                self.stop = True
                self.cond.notify_all()

    def next(self, after, timeout=5):
        end = time.time() + timeout
        with self.cond:
            while self.seq <= after and not self.stop:
                left = end - time.time()
                if left <= 0:
                    return after, None
                self.cond.wait(left)
            return self.seq, (self.jpeg if self.seq > after else None)

    def close(self):
        self.stop = True


def detect(model, keep_ids, im):
    h, w = im.shape[:2]
    t0 = time.time()
    res = model.predict(im, imgsz=IMGSZ, conf=CONF, iou=IOU, classes=keep_ids, device=0, verbose=False)[0]
    dets = []
    for (x1, y1, x2, y2), c, k in zip(res.boxes.xyxy.tolist(), res.boxes.conf.tolist(), res.boxes.cls.tolist()):
        dets.append({"type": model.names[int(k)], "conf": round(c, 3), "qr": None,
                     "box": [round(x1 / w, 4), round(y1 / h, 4), round((x2 - x1) / w, 4), round((y2 - y1) / h, 4)],
                     "_px": (x1, y1, x2, y2)})
    ok, texts, points, _ = qr.detectAndDecodeMulti(im)
    if ok and points is not None:
        for t, p in zip(texts, points):  # each QR goes to the smallest box containing its centre
            cx, cy = float(p[:, 0].mean()), float(p[:, 1].mean())
            inside = [d for d in dets if t and d["qr"] is None and d["_px"][0] <= cx <= d["_px"][2] and d["_px"][1] <= cy <= d["_px"][3]]
            if inside:
                min(inside, key=lambda d: (d["_px"][2] - d["_px"][0]) * (d["_px"][3] - d["_px"][1]))["qr"] = t.strip()
    for d in dets:
        d.pop("_px")
    return {"detections": dets, "duration": round(time.time() - t0, 3), "ts": round(time.time(), 3),
            "image_w": w, "image_h": h}


class Poster:
    """Sends results to KitCheck from its own thread over one kept-alive connection, always the newest one, so a
    slow link (Tailscale round trip ~230 ms to Thailand) never slows detection down."""

    def __init__(self):
        u = urllib.parse.urlsplit(KITCHECK)
        self.host, self.port = u.hostname, u.port or 80
        self.body, self.cond, self.sent = None, threading.Condition(), 0
        threading.Thread(target=self._run, daemon=True).start()

    def put(self, body):
        with self.cond:
            self.body = body
            self.cond.notify()

    def _run(self):
        conn = None
        while True:
            with self.cond:
                while self.body is None:
                    self.cond.wait()
                body, self.body = self.body, None
            data = json.dumps(body).encode()
            for attempt in (1, 2):  # KitCheck drops idle kept-alive connections after 5 s: reconnect and resend
                try:
                    if conn is None:
                        conn = http.client.HTTPConnection(self.host, self.port, timeout=5)
                    conn.request("POST", "/api/station/live", data, {"Content-Type": "application/json"})
                    conn.getresponse().read()
                    self.sent += 1
                    break
                except Exception as e:
                    if conn:
                        conn.close()
                    conn = None
                    if attempt == 2:
                        log("post failed:", repr(e)[:120])
                        time.sleep(0.5)


qr = cv2.QRCodeDetector()


def main():
    from ultralytics import YOLO
    model = YOLO(MODEL)
    keep_ids = [i for i, n in model.names.items() if n in KEEP] or None
    model.predict(np.zeros((720, 1280, 3), np.uint8), imgsz=IMGSZ, device=0, verbose=False)  # warm up CUDA
    log("model ready:", MODEL, "classes kept:", [model.names[i] for i in keep_ids or []])
    wanted = Wanted()
    poster = Poster()
    stream, seq, frames, t_stat = None, 0, 0, time.time()
    while True:
        active = wanted.now or time.time() - wanted.last_true < KEEP_S
        if not active:
            if stream:
                stream.close()
                stream = None
                log("idle: camera stream closed")
            time.sleep(0.2)
            continue
        if stream is None or stream.stop:
            stream, seq = Stream(), 0
            log("scan waiting: camera stream opened")
        seq, jpg = stream.next(seq)
        if not jpg:
            continue
        im = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
        if im is None:
            continue
        if not wanted.now:  # warm stream kept open, but nothing to report
            continue
        out = detect(model, keep_ids, im)
        poster.put(out)
        frames += 1
        if time.time() - t_stat > 5:
            el = time.time() - t_stat
            log("%.1f detections/s, %.1f sent/s, last: %s" % (frames / el, poster.sent / el,
                                                            [(d["type"], d["conf"]) for d in out["detections"]]))
            frames, poster.sent, t_stat = 0, 0, time.time()


if __name__ == "__main__":
    main()
