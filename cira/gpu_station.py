"""Real-time KitCheck detection on the cloud GPU VM, for CiRA CORE (in Docker) and KitCheck on the laptop.

YOLO11n stays loaded on the T4 and checks EVERY frame of the laptop camera (RELAY_FPS, default 10/s); each result
goes to KitCheck's /api/station/live right away, so the web page and the ESP32 screen can draw live boxes and a
borrow/return scan gets its answer from the next frame. QR codes are read only while a scan is waiting (they cost
~60 ms of CPU a frame). Listens on the Docker bridge only (172.17.0.1):

  :8082  GET /stream    the raw camera (password added here) for CiRA's GstCam
  :8083  GET /stream    the same frames with the boxes drawn in, for CiRA's GstCam (live view of the detection)
  :8090  GET /latest    newest result as JSON;  POST /detect  body = JPEG -> result (one-off check)

Settings (/etc/kitcheck-ai.env): KITCHECK_URL, CAMERA_URL, CAMERA_USER, CAMERA_PASS, MODEL, IMGSZ, CONF, IOU,
RELAY_FPS (10), BIND (172.17.0.1).
"""
import base64
import http.client
import json
import os
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cv2
import numpy as np

BIND = os.environ.get("BIND", "172.17.0.1")
KITCHECK = urllib.parse.urlsplit(os.environ.get("KITCHECK_URL", "http://100.101.113.108:8080"))
CAM = urllib.parse.urlsplit(os.environ.get("CAMERA_URL", "http://100.101.113.108:8081"))
CAM_AUTH = "Basic " + base64.b64encode(("%s:%s" % (os.environ.get("CAMERA_USER", "camera"),
                                                     os.environ.get("CAMERA_PASS", ""))).encode()).decode()
RELAY_FPS = float(os.environ.get("RELAY_FPS", "10"))
# localcam/ipcam: /stream?fps=10 (default). go2rtc behind the login proxy: CAMERA_STREAM_PATH=/api/stream.mjpeg?src=cam
CAM_STREAM = os.environ.get("CAMERA_STREAM_PATH") or "/stream?fps=%g" % RELAY_FPS
MODEL = os.path.expanduser(os.environ.get("MODEL", "~/model/last.pt"))
IMGSZ = int(os.environ.get("IMGSZ", "640"))
CONF = float(os.environ.get("CONF", "0.45"))
IOU = float(os.environ.get("IOU", "0.5"))
KEEP = ("hammer", "screwdriver", "pliers", "wrench", "drill")  # the tool types KitCheck lends


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


# ---------------- camera relay ----------------
class Upstream:
    """One connection to the laptop camera while anyone (CiRA) is watching; newest JPEG shared by all viewers."""

    def __init__(self):
        self.cond = threading.Condition()
        self.jpeg, self.seq, self.at, self.viewers = None, 0, 0.0, 0
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while True:
            with self.cond:
                while self.viewers == 0:
                    self.cond.wait()
            if CAM.scheme == "https":
                conn = http.client.HTTPSConnection(CAM.hostname, CAM.port or 443, timeout=20)
            else:  # go2rtc may need ~10 s to start a camera nobody was watching
                conn = http.client.HTTPConnection(CAM.hostname, CAM.port or 80, timeout=20)
            try:
                conn.request("GET", CAM.path.rstrip("/") + CAM_STREAM, headers={"Authorization": CAM_AUTH})
                r = conn.getresponse()
                if r.status != 200:
                    log("camera answered", r.status)
                    time.sleep(3)
                    continue
                log("camera relay: upstream open")
                while self.viewers:
                    line = r.readline(512)
                    if not line:
                        break
                    if not line.lower().startswith(b"content-length"):
                        continue
                    n = int(line.split(b":")[1])
                    while r.readline(512).strip():
                        pass
                    data = r.read(n)
                    with self.cond:
                        self.jpeg, self.seq, self.at = data, self.seq + 1, time.monotonic()
                        self.cond.notify_all()
            except Exception as e:
                log("camera relay:", repr(e)[:120])
                time.sleep(2)
            finally:
                conn.close()

    def frames(self):
        with self.cond:
            self.viewers += 1
            self.cond.notify_all()
        seq = self.seq
        try:
            while True:
                with self.cond:
                    end = time.monotonic() + (2 if RELAY_ONLY else 10)
                    while (self.seq <= seq or time.monotonic() - self.at > 2) and time.monotonic() < end:
                        self.cond.wait(1)
                    if self.seq <= seq:
                        if RELAY_ONLY and self.jpeg:
                            # source gone for a while (e.g. KitCheck restarting): keep CiRA's GstCam fed with the
                            # last picture instead of closing, so its flow loop does not stop
                            jpg = self.jpeg
                        else:
                            return
                    else:
                        seq, jpg = self.seq, self.jpeg
                yield jpg
        finally:
            with self.cond:
                self.viewers -= 1


up = Upstream()


class Annotated:
    """Newest frame with the boxes drawn in (JPEG), produced by the detection loop."""

    def __init__(self):
        self.cond = threading.Condition()
        self.jpeg, self.seq = None, 0

    def put(self, jpg):
        with self.cond:
            self.jpeg, self.seq = jpg, self.seq + 1
            self.cond.notify_all()

    def frames(self):
        seq = self.seq
        while True:
            with self.cond:
                if not self.cond.wait_for(lambda: self.seq > seq, timeout=10):
                    return
                seq, jpg = self.seq, self.jpeg
            yield jpg


annotated = Annotated()


def mjpeg_handler(source):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path.split("?")[0] != "/stream":
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace;boundary=frame")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                for jpg in source():
                    self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n" % len(jpg)
                                     + jpg + b"\r\n")
            except OSError:
                pass
    return H


# ---------------- KitCheck link ----------------
class KitCheck:
    """Posts every result to KitCheck (newest only, own thread, one kept-alive connection) and keeps track of
    whether a scan is waiting (then QR codes are read too)."""

    def __init__(self):
        self.body, self.cond, self.sent, self.wanted = None, threading.Condition(), 0, False
        # 4 senders in parallel: one round trip to Thailand is ~230 ms, so one sender alone manages ~2-4 results/s.
        # KitCheck keeps only the newest result (by ts), so an overtaken one is simply ignored.
        for _ in range(4):
            threading.Thread(target=self._post_loop, daemon=True).start()
        threading.Thread(target=self._wanted_loop, daemon=True).start()

    def put(self, body):
        with self.cond:
            self.body = body
            self.cond.notify()

    def _conn(self):
        return http.client.HTTPConnection(KITCHECK.hostname, KITCHECK.port or 80, timeout=5)

    def _post_loop(self):
        conn = None
        while True:
            with self.cond:
                self.cond.wait_for(lambda: self.body is not None)
                body, self.body = self.body, None
            data = json.dumps(body).encode()
            for attempt in (1, 2):  # KitCheck drops idle kept-alive connections: reconnect and resend
                try:
                    conn = conn or self._conn()
                    conn.request("POST", "/api/station/live", data, {"Content-Type": "application/json"})
                    self.wanted = bool(json.loads(conn.getresponse().read() or b"{}").get("wanted"))
                    self.sent += 1
                    break
                except Exception as e:
                    if conn:
                        conn.close()
                    conn = None
                    if attempt == 2:
                        log("post to KitCheck failed:", repr(e)[:120])
                        time.sleep(1)

    def _wanted_loop(self):  # also asked between posts, in case no frames are flowing
        while True:
            try:
                c = self._conn()
                c.request("GET", "/api/station/wanted")
                self.wanted = bool(json.loads(c.getresponse().read()).get("wanted"))
                c.close()
            except Exception:
                pass
            time.sleep(1)


# RELAY_ONLY=1: only the camera relay on :8082. Detection, QR and posting to KitCheck then run inside CiRA CORE
# (Python+ node, kitcheck_pyplus.py), so this service must not post results too.
RELAY_ONLY = os.environ.get("RELAY_ONLY") == "1"
kc = None if RELAY_ONLY else KitCheck()


# ---------------- detector ----------------
qr = cv2.QRCodeDetector()
lock = threading.Lock()
model = keep_ids = None


latest = {"detections": [], "ts": 0}


def detect(im, read_qr=True):
    h, w = im.shape[:2]
    t0 = time.time()
    with lock:
        res = model.predict(im, imgsz=IMGSZ, conf=CONF, iou=IOU, classes=keep_ids, device=0, verbose=False)[0]
    dets = []
    for (x1, y1, x2, y2), c, k in zip(res.boxes.xyxy.tolist(), res.boxes.conf.tolist(), res.boxes.cls.tolist()):
        dets.append({"type": model.names[int(k)], "conf": round(c, 3), "qr": None,
                     "box": [round(x1 / w, 4), round(y1 / h, 4), round((x2 - x1) / w, 4), round((y2 - y1) / h, 4)],
                     "_px": (x1, y1, x2, y2)})
    ok, texts, points, _ = qr.detectAndDecodeMulti(im) if read_qr else (False, None, None, None)
    if ok and points is not None:
        for t, p in zip(texts, points):  # each QR goes to the smallest box containing its centre
            cx, cy = float(p[:, 0].mean()), float(p[:, 1].mean())
            inside = [d for d in dets if t and d["qr"] is None and d["_px"][0] <= cx <= d["_px"][2] and d["_px"][1] <= cy <= d["_px"][3]]
            if inside:
                min(inside, key=lambda d: (d["_px"][2] - d["_px"][0]) * (d["_px"][3] - d["_px"][1]))["qr"] = t.strip()
    if read_qr:
        # The whole-picture multi detector misses small stickers; look again inside each tool's own box, enlarged.
        taken = {d["qr"] for d in dets if d["qr"]}
        for d in dets:
            if d["qr"]:
                continue
            x1, y1, x2, y2 = d["_px"]
            mx, my = 0.1 * (x2 - x1), 0.1 * (y2 - y1)
            crop = im[max(0, int(y1 - my)):min(h, int(y2 + my)), max(0, int(x1 - mx)):min(w, int(x2 + mx))]
            if crop.size == 0:
                continue
            k = max(1.0, 800.0 / max(crop.shape[:2]))
            if k > 1:
                crop = cv2.resize(crop, None, fx=k, fy=k, interpolation=cv2.INTER_CUBIC)
            try:
                t, _, _ = qr.detectAndDecode(crop)
            except cv2.error:
                t = ""
            if t and t.strip() not in taken:
                d["qr"] = t.strip()
                taken.add(d["qr"])
    for d in dets:
        d["_px"] = tuple(int(v) for v in d["_px"])
    return {"detections": dets, "duration": round(time.time() - t0, 3), "ts": round(time.time(), 3),
            "image_w": w, "image_h": h}


def public(res):
    out = dict(res)
    out["detections"] = [{k: v for k, v in d.items() if k != "_px"} for d in res["detections"]]
    return out


COLORS = {"hammer": (60, 180, 255), "screwdriver": (80, 220, 120), "pliers": (255, 160, 60),
          "wrench": (220, 120, 255), "drill": (80, 200, 255)}


def draw(im, res):
    for d in res["detections"]:
        x1, y1, x2, y2 = d["_px"]
        col = COLORS.get(d["type"], (0, 220, 255))
        cv2.rectangle(im, (x1, y1), (x2, y2), col, 3)
        label = "%s %.0f%%%s" % (d["type"], d["conf"] * 100, (" " + d["qr"]) if d.get("qr") else "")
        (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.7, 2)
        cv2.rectangle(im, (x1, max(0, y1 - th - 10)), (x1 + tw + 8, y1), col, -1)
        cv2.putText(im, label, (x1 + 4, y1 - 6), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (20, 20, 20), 2)
    cv2.putText(im, "GPU %d ms%s" % (res["duration"] * 1000, "  SCAN" if kc.wanted else ""), (12, 30),
                cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2)
    return cv2.imencode(".jpg", im, [cv2.IMWRITE_JPEG_QUALITY, 80])[1].tobytes()


def detect_loop():
    """Every camera frame: detect, post to KitCheck, publish the drawn frame. Runs for as long as the service."""
    global latest
    n, t_stat = 0, time.time()
    while True:
        for jpg in up.frames():
            im = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
            if im is None:
                continue
            res = detect(im, read_qr=kc.wanted)
            latest = public(res)
            kc.put(latest)
            annotated.put(draw(im, res))
            n += 1
            if time.time() - t_stat >= 30:
                el = time.time() - t_stat
                log("%.1f frames/s checked, %.1f results/s sent to KitCheck, last %d ms: %s" % (
                    n / el, kc.sent / el, res["duration"] * 1000, [(d["type"], d["conf"]) for d in res["detections"]]))
                n, kc.sent, t_stat = 0, 0, time.time()
        time.sleep(1)  # camera gone: wait and reopen


class Detect(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path != "/detect":
            return self._send(404, {"error": "not found"})
        n = int(self.headers.get("Content-Length") or 0)
        data = self.rfile.read(n) if 0 < n < 20 * 1024 * 1024 else b""
        im = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR) if data else None
        if im is None:
            return self._send(400, {"error": "send a JPEG/PNG body"})
        self._send(200, public(detect(im)))

    def do_GET(self):
        if self.path != "/latest":
            return self._send(404, {"error": "not found"})
        self._send(200, dict(latest, wanted=kc.wanted))


def main():
    global model, keep_ids
    if RELAY_ONLY:
        srv = ThreadingHTTPServer((BIND, 8082), mjpeg_handler(up.frames))
        srv.daemon_threads = True
        log("relay only: listening on %s:8082 (raw camera); AI runs in CiRA CORE" % BIND)
        srv.serve_forever()
        return
    from ultralytics import YOLO
    model = YOLO(MODEL)
    keep_ids = [i for i, n in model.names.items() if n in KEEP] or None
    model.predict(np.zeros((720, 1280, 3), np.uint8), imgsz=IMGSZ, device=0, verbose=False)  # warm up CUDA
    log("model ready on GPU:", MODEL, [model.names[i] for i in keep_ids or []])
    for port, h, what in ((8082, mjpeg_handler(up.frames), "raw camera"),
                          (8083, mjpeg_handler(annotated.frames), "camera with boxes"),
                          (8090, Detect, "results")):
        srv = ThreadingHTTPServer((BIND, port), h)
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        log("listening on %s:%d (%s)" % (BIND, port, what))
    detect_loop()


if __name__ == "__main__":
    main()
