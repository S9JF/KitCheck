#preparation here
# KitCheck station inside CiRA CORE (Python+ node). Runs ONCE when the node's server starts:
# loads the YOLO11 model (last.pt) onto the T4 GPU, then handle_request() runs for every camera frame:
#   GstCam frame -> YOLO (5 tool types) -> QR codes (only while KitCheck has a scan waiting) -> boxes drawn on
#   the frame for CiRA's view -> newest result posted to KitCheck /api/station/live (background threads).
# Settings: /home/cira/kitcheck_model/station.env (KEY=VALUE), all optional.
import http.client
import json
import os
import sys
import threading
import time
import urllib.parse
from pathlib import Path

import cv2
import numpy as np

# CiRA runs this code on its own, without the template's names: define what we use ourselves.
home_path = str(Path.home())
for _p in (home_path + "/.cira_core_install/cira_libs_ws/install/lib/python3/dist-packages",
           "/opt/ros/noetic/lib/python3/dist-packages"):
    if _p not in sys.path:
        sys.path.append(_p)
# Our own ROS Image <-> numpy (ros_numpy, which CiRA's template uses, is not installed in this container).
from cira_msgs.srv import CiraFlowService2Response
from sensor_msgs.msg import Image as ROSImage

_CH = {"mono8": 1, "8UC1": 1, "bgr8": 3, "rgb8": 3, "8UC3": 3, "bgra8": 4, "rgba8": 4, "8UC4": 4}


def _numpify(m):
    ch = _CH.get(m.encoding, 3)
    a = np.frombuffer(m.data, np.uint8).reshape(m.height, m.step)[:, :m.width * ch].reshape(m.height, m.width, ch)
    if m.encoding == "rgb8":
        a = cv2.cvtColor(a, cv2.COLOR_RGB2BGR)
    elif m.encoding in ("bgra8", "8UC4"):
        a = cv2.cvtColor(a, cv2.COLOR_BGRA2BGR)
    elif m.encoding == "rgba8":
        a = cv2.cvtColor(a, cv2.COLOR_RGBA2BGR)
    elif ch == 1:
        a = a[:, :, 0]
    return np.ascontiguousarray(a)


def _msgify(img):
    m = ROSImage()
    m.height, m.width = img.shape[:2]
    m.encoding = "mono8" if img.ndim == 2 else "bgr8"
    m.is_bigendian = 0
    m.step = m.width * (1 if img.ndim == 2 else 3)
    m.data = np.ascontiguousarray(img, dtype=np.uint8).tobytes()
    return m


def getReqData(req):
    payload = json.loads(req.flow_in.jsonstr or "{}")
    img = _numpify(req.flow_in.img) if len(req.flow_in.img.data) > 0 else None
    return payload, img


def makeRespData(payload, img):
    resp = CiraFlowService2Response()
    resp.flow_out.jsonstr = json.dumps(payload)
    if img is not None and isinstance(img, np.ndarray) and img.ndim in (2, 3):
        resp.flow_out.img = _msgify(img)
    return resp

KC_HOME = home_path + "/kitcheck_model"
CFG = {"KITCHECK_URL": "http://100.101.113.108:8080", "MODEL": KC_HOME + "/last.pt",
       "IMGSZ": "640", "CONF": "0.45", "IOU": "0.5"}
try:
    for _l in open(KC_HOME + "/station.env", encoding="utf-8"):
        _k, _, _v = _l.strip().partition("=")
        if _k and not _k.startswith("#"):
            CFG[_k.strip()] = _v.strip()
except OSError:
    pass
KITCHECK = urllib.parse.urlsplit(CFG["KITCHECK_URL"])
# QR_ALWAYS=1 in station.env: read QR codes on every frame (demo: the codes show on the live boxes all the time;
# costs ~0.1 s per frame). 0 = only while KitCheck has a scan waiting. Re-read every 3 s, no restart needed.
OPT = {"qr_always": CFG.get("QR_ALWAYS", "0") == "1"}


def _watch_cfg():
    while True:
        time.sleep(3)
        try:
            for _l in open(KC_HOME + "/station.env", encoding="utf-8"):
                _k, _, _v = _l.strip().partition("=")
                if _k.strip() == "QR_ALWAYS":
                    OPT["qr_always"] = _v.strip() == "1"
        except OSError:
            pass


threading.Thread(target=_watch_cfg, daemon=True).start()
IMGSZ, CONF, IOU = int(CFG["IMGSZ"]), float(CFG["CONF"]), float(CFG["IOU"])
KEEP = ("hammer", "screwdriver", "pliers", "wrench", "drill")  # the tool types KitCheck lends
COLORS = {"hammer": (60, 180, 255), "screwdriver": (80, 220, 120), "pliers": (255, 160, 60),
          "wrench": (220, 120, 255), "drill": (80, 200, 255)}


class KitCheckLink:
    """Newest result -> KitCheck (4 senders: a round trip to the laptop is ~230 ms). Also tracks whether a
    borrow/return scan is waiting; QR codes are only read then (they cost time)."""

    def __init__(self):
        self.body, self.cond, self.sent, self.wanted = None, threading.Condition(), 0, False
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
            for attempt in (1, 2):
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
                        print("post to KitCheck failed:", repr(e)[:120])
                        time.sleep(1)

    def _wanted_loop(self):
        while True:
            try:
                c = self._conn()
                c.request("GET", "/api/station/wanted")
                self.wanted = bool(json.loads(c.getresponse().read()).get("wanted"))
                c.close()
            except Exception:
                pass
            time.sleep(1)


import torch
from ultralytics import YOLO

print("torch", torch.__version__, "cuda", torch.cuda.is_available())
DEVICE = 0 if torch.cuda.is_available() else "cpu"
model = YOLO(CFG["MODEL"])
keep_ids = [i for i, n in model.names.items() if n in KEEP] or None
model.predict(np.zeros((720, 1280, 3), np.uint8), imgsz=IMGSZ, device=DEVICE, verbose=False)  # warm up
qr = cv2.QRCodeDetector()
kc = KitCheckLink()
stat = {"n": 0, "t": time.time()}
print("KitCheck station ready:", CFG["MODEL"], "on", torch.cuda.get_device_name(0) if DEVICE == 0 else "CPU",
      "->", CFG["KITCHECK_URL"])


def detect(im, read_qr):
    h, w = im.shape[:2]
    t0 = time.time()
    res = model.predict(im, imgsz=IMGSZ, conf=CONF, iou=IOU, classes=keep_ids, device=DEVICE, verbose=False)[0]
    dets = []
    for (x1, y1, x2, y2), c, k in zip(res.boxes.xyxy.tolist(), res.boxes.conf.tolist(), res.boxes.cls.tolist()):
        dets.append({"type": model.names[int(k)], "conf": round(c, 3), "qr": None,
                     "box": [round(x1 / w, 4), round(y1 / h, 4), round((x2 - x1) / w, 4), round((y2 - y1) / h, 4)],
                     "_px": (x1, y1, x2, y2)})
    if read_qr and dets:
        ok, texts, points, _ = qr.detectAndDecodeMulti(im)
        if ok and points is not None:
            for t, p in zip(texts, points):  # each QR goes to the smallest box containing its centre
                cx, cy = float(p[:, 0].mean()), float(p[:, 1].mean())
                inside = [d for d in dets if t and d["qr"] is None
                          and d["_px"][0] <= cx <= d["_px"][2] and d["_px"][1] <= cy <= d["_px"][3]]
                if inside:
                    min(inside, key=lambda d: (d["_px"][2] - d["_px"][0]) * (d["_px"][3] - d["_px"][1]))["qr"] = t.strip()
        taken = {d["qr"] for d in dets if d["qr"]}
        for d in dets:  # small stickers: look again inside each tool's own box, enlarged
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
    return {"detections": dets, "duration": round(time.time() - t0, 3), "ts": round(time.time(), 3),
            "image_w": w, "image_h": h, "qr_read": bool(read_qr)}


def draw(im, res):
    for d in res["detections"]:
        x1, y1, x2, y2 = (int(v) for v in d["_px"])
        col = COLORS.get(d["type"], (0, 220, 255))
        cv2.rectangle(im, (x1, y1), (x2, y2), col, 3)
        label = "%s %.0f%%%s" % (d["type"], d["conf"] * 100, (" " + d["qr"]) if d.get("qr") else "")
        (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.7, 2)
        cv2.rectangle(im, (x1, max(0, y1 - th - 10)), (x1 + tw + 8, y1), col, -1)
        cv2.putText(im, label, (x1 + 4, y1 - 6), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (20, 20, 20), 2)
    cv2.putText(im, "CiRA Python+ GPU %d ms%s%s" % (res["duration"] * 1000, "  SCAN" if kc.wanted else "",
                                                   "  QR:every frame" if OPT["qr_always"] else ""),
                (12, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2)
    return im


def handle_request(req):
    payload, img = getReqData(req)
    if img is None:
        payload["kitcheck"] = {"error": "no image from the camera node"}
        return makeRespData(payload, img)
    if img.ndim == 2:
        img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    res = detect(img, read_qr=kc.wanted or OPT["qr_always"])
    public = dict(res, detections=[{k: v for k, v in d.items() if k != "_px"} for d in res["detections"]])
    kc.put(public)
    # CiRA's flow data is {"payload": {...}}: put the result there, so Debug and MqttPub (next nodes) carry it
    P = payload["payload"] if isinstance(payload.get("payload"), dict) else payload
    P["kitcheck"] = dict(public, scan_waiting=kc.wanted,
                               counts={t: sum(1 for d in res["detections"] if d["type"] == t) for t in KEEP})
    stat["n"] += 1
    if time.time() - stat["t"] >= 30:
        el = time.time() - stat["t"]
        print("%.1f frames/s, %.1f results/s sent to KitCheck" % (stat["n"] / el, kc.sent / el))
        stat["n"], kc.sent, stat["t"] = 0, 0, time.time()
    out = draw(img.copy(), res)
    if time.time() - stat.get("saved", 0) >= 1.0:  # newest result + picture for the LINE chatbot node ("ถาด")
        stat["saved"] = time.time()
        try:
            cv2.imwrite(KC_HOME + "/latest.jpg.tmp.jpg", out, [cv2.IMWRITE_JPEG_QUALITY, 80])
            os.replace(KC_HOME + "/latest.jpg.tmp.jpg", KC_HOME + "/latest.jpg")
            with open(KC_HOME + "/latest.json.tmp", "w", encoding="utf-8") as f:
                json.dump(public, f)
            os.replace(KC_HOME + "/latest.json.tmp", KC_HOME + "/latest.json")
        except OSError:
            pass
    return makeRespData(payload, out)
