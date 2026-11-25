"""HTTP routing shared by the FastAPI app (main.py) and the no-dependency dev server (devserver.py)."""
import html
import io
import json
import os
import re
import secrets
import threading
import time
from base64 import b64encode
from http.client import HTTPConnection, HTTPException, HTTPSConnection
from urllib.parse import urlsplit

from .core import Err, Store

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")

LIVE_BOX_MAX_AGE_S = 2.0  # older CiRA boxes are not drawn on the live view

# The part of the camera frame that is the tray (x0,y0,x1,y1 as 0..1 of the frame). Views show only this
# part and detections whose centre lies outside it (things next to the tray) are ignored.
# Measured on the searserver rig; override with KITCHECK_CAMERA_CROP=x0,y0,x1,y1, "0,0,1,1" turns it off.
# (Finding the pad automatically was tried and dropped: it picked wrong regions and cost CPU.)
DEFAULT_CAMERA_CROP = "0.085,0.175,0.78,0.90"  # just the black pad


def _env(name, default=None):
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def _parse_crop(s):
    try:
        x0, y0, x1, y1 = (float(v) for v in s.split(","))
    except (AttributeError, ValueError):
        return None
    x0, y0, x1, y1 = max(0.0, x0), max(0.0, y0), min(1.0, x1), min(1.0, y1)
    if x1 - x0 < 0.05 or y1 - y0 < 0.05 or (x0, y0, x1, y1) == (0.0, 0.0, 1.0, 1.0):
        return None
    return (x0, y0, x1, y1)


def _credential(name):
    """A systemd LoadCredential= secret (read from $CREDENTIALS_DIRECTORY), or None."""
    d = os.environ.get("CREDENTIALS_DIRECTORY")
    if not d:
        return None
    try:
        with open(os.path.join(d, name), "rb") as f:
            return f.read().strip().decode("utf-8")
    except OSError:
        return None


class _FirstFrame:
    """Looks like an HTTP response whose body is one JPEG: the first part of an MJPEG stream."""

    def __init__(self, r):
        self.r = r

    def read(self, *_):
        r = self.r
        for _ in range(64):
            line = r.readline(512)
            if not line:
                break
            if not line.startswith(b"--"):
                continue
            length = None
            while True:
                h = r.readline(2048).strip()
                if not h:
                    break
                k, _, v = h.partition(b":")
                if k.strip().lower() == b"content-length":
                    length = int(v.strip())
            if length and length <= 8 * 1024 * 1024:
                data = r.read(length)
                if len(data) == length:
                    return data
            break
        raise HTTPException("no frame in the camera stream")


class _Bytes:
    def __init__(self, data):
        self.data = data

    def read(self, *_):
        return self.data


class _Done:
    def close(self):
        pass


_BLANK = []


def _blank_jpeg():
    """An empty dark 1280x720 picture (the virtual camera before the /sim page sends anything)."""
    if not _BLANK:
        try:
            import cv2
            import numpy as np
            _BLANK.append(cv2.imencode(".jpg", np.full((720, 1280, 3), 28, np.uint8))[1].tobytes())
        except ImportError:
            from io import BytesIO
            from PIL import Image
            buf = BytesIO()
            Image.new("RGB", (1280, 720), (28, 28, 28)).save(buf, "JPEG")
            _BLANK.append(buf.getvalue())
    return _BLANK[0]


class VirtualCam:
    """Newest frame of the virtual tray, posted by the /sim page; read like a camera."""

    def __init__(self):
        self.cond = threading.Condition()
        self.jpeg, self.seq, self.at = None, 0, 0.0

    def put(self, jpg):
        with self.cond:
            self.jpeg, self.seq, self.at = jpg, self.seq + 1, time.monotonic()
            self.cond.notify_all()

    def latest(self, timeout=3.0):
        """Newest frame if it is at most 3 s old (waits up to timeout for one)."""
        with self.cond:
            self.cond.wait_for(lambda: self.jpeg is not None and time.monotonic() - self.at < 3, timeout=timeout)
            return self.jpeg if self.jpeg is not None and time.monotonic() - self.at < 3 else None

    def frames(self, idle=10.0):
        """Every new frame; ends when none arrived for idle seconds."""
        seq = self.seq
        while True:
            with self.cond:
                if not self.cond.wait_for(lambda: self.seq > seq, timeout=idle):
                    return
                seq, jpg = self.seq, self.jpeg
            yield jpg


class _VirtualStream:
    """Looks like the HTTP response of an MJPEG camera (readline/read), fed from VirtualCam."""

    def __init__(self, cam):
        self.it = self._never_idle(cam)
        self.buf = b""

    @staticmethod
    def _never_idle(cam):
        """New frames as they come; with the /sim page closed, the last frame (or a blank one) every 0.5 s, so
        viewers such as the ESP32 kiosk keep a live stream instead of waiting on a silent one."""
        seq = cam.seq
        while True:
            with cam.cond:
                cam.cond.wait_for(lambda: cam.seq > seq, timeout=0.5)
                seq, jpg = cam.seq, cam.jpeg
            yield jpg or _blank_jpeg()

    def _fill(self, n):
        while len(self.buf) < n:
            jpg = next(self.it, None)
            if jpg is None:
                return False
            self.buf += b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n" % len(jpg) + jpg + b"\r\n"
        return True

    def readline(self, limit=-1):
        while b"\n" not in self.buf[:limit if limit > 0 else None]:
            if limit > 0 and len(self.buf) >= limit:
                break
            if not self._fill(len(self.buf) + 1):
                break
        i = self.buf.find(b"\n")
        end = i + 1 if i >= 0 else len(self.buf)
        if limit > 0:
            end = min(end, limit)
        out, self.buf = self.buf[:end], self.buf[end:]
        return out

    def read(self, n=-1):
        self._fill(n)
        out, self.buf = self.buf[:n], self.buf[n:]
        return out


class Api:
    def __init__(self):
        self.data_dir = os.path.abspath(_env("KITCHECK_DATA", os.path.join(os.getcwd(), "data")))
        self.img_dir = os.path.join(self.data_dir, "images")
        os.makedirs(self.img_dir, exist_ok=True)
        self.mode = _env("KITCHECK_MODE", "sim")
        self.station_key = _env("KITCHECK_STATION_KEY", "")
        self.store = Store(os.path.join(self.data_dir, "kitcheck.db"), mode=self.mode)
        from .desk import Desk
        self.desk = Desk(self.store, sim=self.mode != "mqtt")
        self.bridge = None
        # KITCHECK_VIRTUAL=1: virtual hardware for demos. The /sim page draws the tray (tools with their QR
        # stickers) and is the camera and the scale; the AI (CiRA) and the rest of KitCheck are the real thing.
        self.virtual = _env("KITCHECK_VIRTUAL") == "1"
        self.desk.virtual = self.virtual
        self.vcam = VirtualCam() if self.virtual else None
        self.cam_crop = None if self.virtual else _parse_crop(_env("KITCHECK_CAMERA_CROP", DEFAULT_CAMERA_CROP))
        # The tray's 4 corners (set on the /tray page): when set, views show the tray straightened out
        # (perspective warp) and AI boxes are mapped onto it; the rectangular crop above is then not used.
        # The virtual tray already fills the picture, so it keeps its own (empty) setting.
        from .tray import Tray
        tray_dir = os.path.join(self.data_dir, "virtual") if self.virtual else self.data_dir
        os.makedirs(tray_dir, exist_ok=True)
        self.tray = Tray(tray_dir)
        # MQTT link: always in "mqtt" mode; in "sim" mode too when KITCHECK_MQTT_ENABLE=1, so the
        # tray scale (ESP32) and station status arrive while camera detections are still simulated.
        if self.mode == "mqtt" or _env("KITCHECK_MQTT_ENABLE", "0") == "1":
            from .mqtt_bridge import Bridge
            self.bridge = Bridge(
                self.store,
                host=_env("KITCHECK_MQTT_HOST", "127.0.0.1"),
                port=int(_env("KITCHECK_MQTT_PORT", "1883")),
                prefix=_env("KITCHECK_MQTT_PREFIX", "kitcheck"),
                username=_env("KITCHECK_MQTT_USER"),
                password=_env("KITCHECK_MQTT_PASS"),
                desk=self.desk,
                crop=self.cam_crop,
                tray=self.tray,
            )
        if self.bridge:
            self.store.on_event = self.bridge.publish_event
        self._sim_scale_at = 0.0
        # Live camera: KitCheck proxies the ustreamer MJPEG stream (which needs Basic auth and is only on
        # the LAN) to logged-in browsers, and serves CiRA's latest boxes separately; the browser draws them.
        cam = urlsplit(_env("KITCHECK_CAMERA_URL", ""))
        self.cam_host = cam.hostname
        self.cam_tls = cam.scheme == "https"
        self.cam_port = cam.port or (443 if self.cam_tls else 80)
        self.cam_base = cam.path.rstrip("/")
        # ustreamer / localcam: /snapshot and /stream. go2rtc (behind the "ipcam" login proxy):
        #   KITCHECK_CAMERA_SNAPSHOT_PATH=/api/frame.jpeg?src=cam  KITCHECK_CAMERA_STREAM_PATH=/api/stream.mjpeg?src=cam
        self.cam_paths = {"/snapshot": _env("KITCHECK_CAMERA_SNAPSHOT_PATH", "/snapshot"),
                          "/stream": _env("KITCHECK_CAMERA_STREAM_PATH", "/stream")}
        self.cam_last = (0.0, None)  # (monotonic time, JPEG) from the keep-alive stream
        cam_pass = _credential("camera-password") or _env("KITCHECK_CAMERA_PASS")
        cam_user = _env("KITCHECK_CAMERA_USER", "camera")
        self.cam_auth = ("Basic " + b64encode(f"{cam_user}:{cam_pass}".encode()).decode()) if cam_pass else None
        self.cam_fps = max(1.0, float(_env("KITCHECK_CAMERA_FPS", "8")))
        self.cam_max = int(_env("KITCHECK_CAMERA_MAX_VIEWERS", "4"))
        self.cam_streams = {}  # stream id -> monotonic time it last sent a frame
        self.cam_lock = threading.Lock()
        if self.virtual:
            self.cam_host, self.cam_auth = "virtual", "virtual"  # "a camera is configured"
        elif self.cam_host and self.cam_auth and _env("KITCHECK_CAMERA_KEEPALIVE") == "1":
            threading.Thread(target=self._cam_keep, name="cam-keepalive", daemon=True).start()
        self._names = ({}, 0.0)
        self.routes = [
            ("GET", r"/", self.index),
            ("GET", r"/index\.html", self.index),
            ("GET", r"/kiosk", self.kiosk_page),
            ("GET", r"/livecam\.js", self.livecam_js),
            ("GET", r"/tray", self.tray_page),
            ("GET", r"/sim", self.sim_page),
            ("GET", r"/sim/([a-z_]+\.(?:png|json))", self.sim_asset),
            ("GET", r"/api/virtual/info", self.virtual_info),
            ("POST", r"/api/virtual/frame", self.virtual_frame),
            ("POST", r"/api/virtual/scale", self.virtual_scale),
            ("GET", r"/api/virtual/qr/([A-Za-z0-9_.-]{1,64})\.png", self.virtual_qr),
            ("GET", r"/api/camera/tray", self.tray_get),
            ("POST", r"/api/camera/tray", self.tray_set),
            ("POST", r"/api/camera/tray/auto", self.tray_auto),
            ("GET", r"/api/camera/live", self.camera_live),
            ("GET", r"/api/camera/snapshot", self.camera_snapshot),
            ("GET", r"/api/camera/small", self.camera_small),
            ("GET", r"/api/station/wanted", self.station_wanted),
            ("GET", r"/api/station/summary", self.station_summary),
            ("POST", r"/api/station/live", self.station_live),
            # ---- v2: self-service kiosk, per-item loans ----
            ("GET", r"/api/kiosk/state", self.kiosk_state),
            ("POST", r"/api/kiosk/member", self.kiosk_member),
            ("POST", r"/api/kiosk/scan", self.kiosk_scan),
            ("GET", r"/api/kiosk/scan/(ds_\d+)", self.kiosk_scan_get),
            ("POST", r"/api/kiosk/scan/(ds_\d+)/confirm", self.kiosk_confirm),
            ("POST", r"/api/kiosk/scan/(ds_\d+)/cancel", self.kiosk_cancel),
            ("POST", r"/api/sim/tray", self.sim_tray),
            ("GET", r"/api/desk/state", self.desk_state),
            ("POST", r"/api/tools", self.tool_create),
            ("PATCH", r"/api/tools/(ITEM-\d+)", self.tool_update),
            ("POST", r"/api/tools/(ITEM-\d+)/reissue", self.tool_reissue),
            ("POST", r"/api/members", self.member_create),
            ("PATCH", r"/api/members/(\d+)", self.member_update),
            ("POST", r"/api/desk/scans/(ds_\d+)/resolve", self.desk_resolve),
            ("POST", r"/api/desk/loans/(\d+)/lost", self.desk_lost),
            ("POST", r"/api/station/scale", self.station_scale),
            ("GET", r"/api/health", self.health),
            ("GET", r"/(favicon\.svg|favicon\.ico|apple-touch-icon\.png|icon-512\.png)", self.icon),
            ("POST", r"/api/login", self.login),
            ("POST", r"/api/logout", self.logout),
            ("GET", r"/api/state", self.get_state),
            ("POST", r"/api/scans", self.start_scan),
            ("POST", r"/api/scans/(scan_\d+)/edits", self.add_edit),
            ("POST", r"/api/scans/(scan_\d+)/cancel", self.cancel_scan),
            ("POST", r"/api/scans/((?:scan|ds)_\d+)/result", self.station_result),
            ("POST", r"/api/scans/((?:scan|ds)_\d+)/image", self.station_image),
            ("GET", r"/api/scans/((?:scan|ds)_\d+)/image", self.get_image),
            ("POST", r"/api/borrow", self.borrow),
            ("POST", r"/api/return", self.do_return),
            ("POST", r"/api/anomaly", self.anomaly),
            ("POST", r"/api/loans/(LOAN-\d+)/settle", self.settle),
            ("POST", r"/api/kits", self.create_kit),
            ("PATCH", r"/api/kits/(KIT-\d+)", self.update_kit),
            ("POST", r"/api/items/(ITEM-\d+)/reissue", self.reissue),
            ("GET", r"/api/qr/([A-Z0-9-]{1,40})\.svg", self.qr_svg),
            ("GET", r"/labels/(KIT-\d+)", self.labels),
            ("GET", r"/api/users", self.users),
            ("POST", r"/api/users", self.create_user),
        ]
        self.routes = [(m, re.compile("^" + p + "$"), f) for m, p, f in self.routes]

    # ---------- plumbing ----------
    def handle(self, method, path, query, headers, body, client=""):
        try:
            for m, rx, fn in self.routes:
                mt = rx.match(path)
                if mt and m == method:
                    req = {"query": query, "headers": headers, "body": body, "client": client, "args": mt.groups()}
                    return fn(req)
            return self._json(404, {"error": "ไม่พบหน้า"})
        except Err as e:
            return self._json(e.status, {"error": e.msg})
        except (ValueError, KeyError, TypeError) as e:
            return self._json(400, {"error": f"ข้อมูลไม่ถูกต้อง ({type(e).__name__})"})

    def _json(self, status, obj):
        return status, {"Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store"}, json.dumps(obj, ensure_ascii=False).encode("utf-8")

    def _body(self, req):
        if not req["body"]:
            return {}
        try:
            v = json.loads(req["body"].decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise Err(400, "รูปแบบข้อมูลไม่ถูกต้อง")
        if not isinstance(v, dict):
            raise Err(400, "รูปแบบข้อมูลไม่ถูกต้อง")
        return v

    def _token(self, req):
        auth = req["headers"].get("authorization", "")
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        return req["query"].get("t", "")

    def _user(self, req):
        u = self.store.user_by_token(self._token(req))
        if not u:
            raise Err(401, "กรุณาเข้าสู่ระบบ")
        return u

    def _station(self, req):
        key = req["headers"].get("x-station-key", "")
        if not self.station_key or not secrets.compare_digest(key, self.station_key):
            raise Err(403, "station key ไม่ถูกต้อง")

    def _ok_state(self, user, extra=None):
        out = {"ok": True, "state": self.store.state(user)}
        if extra:
            out.update(extra)
        return self._json(200, out)

    # ---------- pages ----------
    def index(self, req):
        with open(os.path.join(STATIC, "index.html"), "rb") as f:
            return 200, {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-cache"}, f.read()

    def health(self, req):
        return self._json(200, {"ok": True, "mode": self.mode, "mqtt": self.store.mqtt_connected,
                                "scale": self.desk.scale_view(), "camera": bool(self.cam_host and self.cam_auth),
                                "cira_live_age_s": self.bridge.live_age() if self.bridge else None})

    # ---------- live camera ----------
    ICON_TYPES = {"svg": "image/svg+xml", "ico": "image/x-icon", "png": "image/png"}

    def icon(self, req):
        name = req["args"][0]
        try:
            with open(os.path.join(STATIC, name), "rb") as f:
                data = f.read()
        except OSError:
            return 204, {}, b""
        return 200, {"Content-Type": self.ICON_TYPES[name.rsplit(".", 1)[1]], "Cache-Control": "public, max-age=604800"}, data

    def livecam_js(self, req):
        with open(os.path.join(STATIC, "livecam.js"), "rb") as f:
            return 200, {"Content-Type": "text/javascript; charset=utf-8", "Cache-Control": "no-cache"}, f.read()

    def camera_info(self):
        return {"camera": bool(self.cam_host and self.cam_auth),
                "ai_age_s": self.bridge.live_age() if self.bridge else None,
                "crop": list(self.cam_crop) if self.cam_crop else None,
                "tray": self.tray.info() if self.tray.active else None}

    # ---------- tray corners (perspective) ----------
    def tray_page(self, req):
        with open(os.path.join(STATIC, "tray.html"), "rb") as f:
            return 200, {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-cache"}, f.read()

    # ---------- virtual hardware (/sim) ----------
    def sim_page(self, req):
        with open(os.path.join(STATIC, "sim.html"), "rb") as f:
            return 200, {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-cache"}, f.read()

    def sim_asset(self, req):
        name = req["args"][0]
        try:
            with open(os.path.join(STATIC, "sim", name), "rb") as f:
                data = f.read()
        except OSError:
            return self._json(404, {"error": "ไม่พบไฟล์"})
        ctype = "image/png" if name.endswith(".png") else "application/json"
        return 200, {"Content-Type": ctype, "Cache-Control": "max-age=3600"}, data

    def _virtual_on(self):
        if not self.virtual:
            raise Err(409, "KitCheck ไม่ได้เปิดโหมดฮาร์ดแวร์จำลอง (KITCHECK_VIRTUAL=1)")

    def virtual_info(self, req):
        self._user(req)
        return self._json(200, {"virtual": self.virtual, "tools": self.desk.catalog(),
                                "station": bool(self.bridge and self._live_fresh())})

    def _live_fresh(self):
        age = self.bridge.live_age() if self.bridge else None
        return age is not None and age < 5

    def _user_or_station(self, req):
        """A logged-in user (the /sim page), or a test script on this PC / the AI station."""
        try:
            self._user(req)
        except Err:
            self._station_only(req)

    def virtual_frame(self, req):
        self._user_or_station(req)
        self._virtual_on()
        data = req["body"] or b""
        if data[:3] != b"\xff\xd8\xff" or len(data) > 4 * 1024 * 1024:
            raise Err(400, "ต้องเป็นภาพ JPEG")
        self.vcam.put(data)
        return self._json(200, {"ok": True})

    def virtual_scale(self, req):
        self._user_or_station(req)
        self._virtual_on()
        b = self._body(req)
        self.desk.set_scale(b.get("grams"), b.get("stable"), src="virtual")
        if self.bridge and time.monotonic() - self._sim_scale_at >= 1.0:  # to MQTT at most once a second
            self._sim_scale_at = time.monotonic()
            self.bridge.publish_sim_scale(self.desk.scale["grams"], b.get("stable"))
        return self._json(200, {"ok": True, "scale": self.desk.scale_view()})

    def virtual_qr(self, req):
        self._user(req)
        import io
        import qrcode
        q = qrcode.QRCode(border=2, box_size=8, error_correction=qrcode.constants.ERROR_CORRECT_M)
        q.add_data(req["args"][0])
        q.make(fit=True)
        buf = io.BytesIO()
        q.make_image(fill_color="black", back_color="white").save(buf, format="PNG")
        return 200, {"Content-Type": "image/png", "Cache-Control": "max-age=3600"}, buf.getvalue()

    def virtual_stream(self, client):
        """For main.py: the virtual tray as an MJPEG stream for the AI station (gpu_station relay -> CiRA)."""
        try:
            self._station_only({"client": client})
            self._virtual_on()
        except Err as e:
            return self._json(e.status, {"error": e.msg})

        def gen():
            # Never goes quiet: while the /sim page sends nothing, the last frame (or an empty tray) is repeated
            # twice a second, so CiRA's GstCam does not time out and stop.
            seq = -1
            while True:
                with self.vcam.cond:
                    self.vcam.cond.wait_for(lambda: self.vcam.seq != seq, timeout=0.5)
                    seq, jpg = self.vcam.seq, self.vcam.jpeg
                jpg = jpg or _blank_jpeg()
                yield b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n" % len(jpg) + jpg + b"\r\n"
        return 200, {"Content-Type": "multipart/x-mixed-replace; boundary=frame", "Cache-Control": "no-store"}, gen()

    def tray_get(self, req):
        self._user(req)
        return self._json(200, {"tray": self.tray.info() if self.tray.quad else None,
                                "warp_available": self.tray.active or bool(self._cv())})

    @staticmethod
    def _cv():
        try:
            import cv2  # noqa: F401
            return True
        except ImportError:
            return False

    def _admin(self, req):
        u = self._user(req)
        if u["role"] != "admin":
            raise Err(403, "เฉพาะผู้ดูแลระบบ")
        return u

    def tray_auto(self, req):
        """Find the tray's corners in a fresh camera frame (a suggestion; nothing is saved)."""
        self._admin(req)
        if not self._cv():
            raise Err(503, "เครื่องนี้ไม่มี OpenCV (pip install opencv-python-headless)")
        import cv2
        import numpy as np
        from .tray import auto_quad
        conn, r = self._cam_get("/snapshot", 6)
        try:
            data = r.read()
        finally:
            conn.close()
        img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            raise Err(502, "อ่านภาพจากกล้องไม่ได้")
        q = auto_quad(img)
        if not q:
            raise Err(422, "หาถาดในภาพไม่เจอ ลากมุมทั้ง 4 เองได้เลย")
        return self._json(200, {"quad": q, "frame": [img.shape[1], img.shape[0]]})

    def tray_set(self, req):
        """{"quad": [[x,y] x4] (0..1 of the frame), "frame": [w,h]} saves the corners; {"clear": true} removes them."""
        self._admin(req)
        b = self._body(req)
        if b.get("clear"):
            self.tray.clear()
            return self._json(200, {"tray": None})
        if "enhance" in b and "quad" not in b:  # only switch the "black pad stands out" colour fix on/off
            self.tray.set_enhance(b["enhance"])
            return self._json(200, {"tray": self.tray.info() if self.tray.quad else None})
        if not self._cv():
            raise Err(503, "เครื่องนี้ไม่มี OpenCV (pip install opencv-python-headless)")
        try:
            self.tray.save(b.get("quad"), b.get("frame"))
        except (ValueError, TypeError, KeyError, IndexError):
            raise Err(400, "มุมถาดไม่ถูกต้อง ต้องมี 4 จุด")
        return self._json(200, {"tray": self.tray.info()})

    def _type_names(self):
        names, at = self._names
        if time.time() - at > 30:
            with self.store.lock:
                names = self.desk._types(self.store.db)
            self._names = (names, time.time())
        return names

    def _live_dets(self):
        live = self.bridge.live if self.bridge else None
        dets = []
        if live and time.time() - live["at"] <= LIVE_BOX_MAX_AGE_S:
            names = self._type_names()
            for d in live["detections"]:  # already limited to the tray by the bridge
                if isinstance(d, dict) and isinstance(d.get("box"), list) and len(d["box"]) == 4:
                    dets.append({"type": d.get("type"), "type_name": names.get(d.get("type"), d.get("type")),
                                 "conf": d.get("conf"), "qr": d.get("qr"), "box": d["box"]})
        return dets

    def camera_live(self, req):
        """Latest CiRA detections (boxes 0..1 of the frame) for the browser to draw over the live video."""
        self._user(req)
        out = self.camera_info()
        out["detections"] = self._live_dets()
        out["scan_waiting"] = bool(self.bridge and self.bridge.wanted())  # CiRA reads QR codes then
        live = self.bridge.live if self.bridge else None
        out["qr_read"] = bool(live and live.get("qr_read")) or out["scan_waiting"]  # ... or always (QR_ALWAYS)
        return self._json(200, out)

    def station_wanted(self, req):
        """For the CiRA script (Docker network only, no login): is a borrow/return scan waiting for a result?
        CiRA asks this every frame and runs YOLO only when it is, instead of on every camera frame."""
        self._station_only(req)
        return self._json(200, {"wanted": bool(self.bridge and self.bridge.wanted())})

    @staticmethod
    def _station_only(req):
        """The AI station, no login: CiRA in Docker on the server (172.17.*), anything on the same PC (loopback),
        or the IPs in KITCHECK_STATION_IPS (comma-separated, e.g. the GPU VM's Tailscale address)."""
        c = req.get("client") or ""
        extra = [s.strip() for s in _env("KITCHECK_STATION_IPS", "").split(",") if s.strip()]
        if not (c.startswith("172.17.") or c in ("127.0.0.1", "::1") or c in extra):
            raise Err(403, "เฉพาะจุดตรวจ AI")

    def station_summary(self, req):
        """For CiRA's LINE bot (AI station only, no login): what is out on loan, and the tray scale."""
        self._station_only(req)
        return self._json(200, {"loans": self.desk.open_loans(), "scale": self.desk.scale_view(),
                                "virtual": self.virtual})

    def station_live(self, req):
        """CiRA on this PC posts each frame's result here (same JSON as the MQTT topic <prefix>/station/live)."""
        self._station_only(req)
        if not self.bridge:
            raise Err(503, "KitCheck ไม่ได้อยู่โหมด mqtt")
        self.bridge.on_live(self._body(req))
        return self._json(200, {"ok": True, "wanted": self.bridge.wanted()})

    def camera_small(self, req):
        """The tray part of a snapshot, downscaled for the ESP32 kiosk screen (?w=592&h=333&q=70): the crop is
        fitted inside w x h and padded to exactly that size. CiRA's latest boxes ride along in the X-Detections
        header as 0..1 of the returned picture, so one request gives both. Without Pillow: the full frame."""
        self._user(req)
        w, h, q = self._small_args(req["query"])
        conn, r = self._cam_get("/snapshot", 6)
        try:
            data = r.read()
        except (OSError, HTTPException):
            raise Err(502, "อ่านภาพจากกล้องไม่ได้")
        finally:
            conn.close()
        data, hdr = self._small_frame(data, w, h, q)
        return 200, {"Content-Type": "image/jpeg", "Cache-Control": "no-store", "X-Detections": hdr}, data

    @staticmethod
    def _small_args(query):
        try:
            w = min(1280, max(160, int(query.get("w", 640))))
            h = query.get("h")
            h = min(960, max(90, int(h))) if h else None
            q = min(90, max(30, int(query.get("q", 70))))
        except ValueError:
            raise Err(400, "w/h/q ไม่ถูกต้อง")
        return w, h, q

    def _small_frame(self, data, w, h, q):
        """Full camera JPEG -> (tray-only JPEG fitted in w x h, CiRA boxes as JSON in 0..1 of that picture)."""
        dets = self._live_dets()  # boxes as 0..1 of the straightened tray (corners set) or of the full frame
        if self.tray.active:
            return self._small_frame_warped(data, w, h, q, dets)
        try:
            from PIL import Image
        except ImportError:
            Image = None
        if Image:
            im = Image.open(io.BytesIO(data))
            fw, fh = im.size
            c = self.cam_crop or (0.0, 0.0, 1.0, 1.0)
            cw, ch = (c[2] - c[0]) * fw, (c[3] - c[1]) * fh
            box_h = h or round(w * ch / cw)
            s = min(w / cw, box_h / ch)
            im.draft("RGB", (max(1, round(fw * s)), max(1, round(fh * s))))  # libjpeg decodes at 1/2, 1/4…: cheap
            k = im.width / fw  # draft may have shrunk the frame
            im = im.convert("RGB").crop((round(c[0] * fw * k), round(c[1] * fh * k),
                                         round(c[2] * fw * k), round(c[3] * fh * k)))
            iw, ih = max(1, round(cw * s)), max(1, round(ch * s))
            im = im.resize((iw, ih), Image.BILINEAR)
            ox, oy = (w - iw) // 2, (box_h - ih) // 2
            if (iw, ih) != (w, box_h):
                canvas = Image.new("RGB", (w, box_h), (28, 30, 33))
                canvas.paste(im, (ox, oy))
                im = canvas
            out = io.BytesIO()
            im.save(out, "JPEG", quality=q)
            data = out.getvalue()
            for d in dets:  # full-frame 0..1 -> 0..1 of the returned picture
                b = d["box"]
                d["box"] = [(ox + (b[0] - c[0]) / (c[2] - c[0]) * iw) / w, (oy + (b[1] - c[1]) / (c[3] - c[1]) * ih) / box_h,
                            b[2] / (c[2] - c[0]) * iw / w, b[3] / (c[3] - c[1]) * ih / box_h]
        return data, json.dumps(dets, ensure_ascii=True, separators=(",", ":"))

    def _small_frame_warped(self, data, w, h, q, dets):
        """The tray straightened out (perspective warp from its 4 corners), fitted inside w x h."""
        import cv2
        import numpy as np
        a = self.tray.aspect
        box_h = h or max(1, round(w / a))
        iw, ih = (w, max(1, round(w / a))) if w / a <= box_h else (max(1, round(box_h * a)), box_h)
        # decode at 1/2 or 1/4 size when that is still bigger than the output (much faster)
        flag = cv2.IMREAD_COLOR
        if iw * 4 <= 1280 * 0.9:
            flag = cv2.IMREAD_REDUCED_COLOR_2
        img = cv2.imdecode(np.frombuffer(data, np.uint8), flag)
        if img is None:
            return data, "[]"
        out = self.tray.look(self.tray.warp(img, iw, ih))
        ox, oy = (w - iw) // 2, (box_h - ih) // 2
        if (iw, ih) != (w, box_h):
            canvas = np.empty((box_h, w, 3), np.uint8)
            canvas[:] = (33, 30, 28)
            canvas[oy:oy + ih, ox:ox + iw] = out
            out = canvas
        jpg = cv2.imencode(".jpg", out, [cv2.IMWRITE_JPEG_QUALITY, q])[1].tobytes()
        for d in dets:  # tray 0..1 -> 0..1 of the returned picture
            b = d["box"]
            d["box"] = [(ox + b[0] * iw) / w, (oy + b[1] * ih) / box_h, b[2] * iw / w, b[3] * ih / box_h]
        return jpg, json.dumps(dets, ensure_ascii=True, separators=(",", ":"))

    def _tray_photo(self, data):
        """Evidence photo: the straightened tray (so the saved boxes line up with it), else the frame as it is."""
        if not (self.tray.active and data):
            return data
        try:
            import cv2
            import numpy as np
            img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
            if img is None:
                return data
            ow = min(1280, img.shape[1])
            out = self.tray.look(self.tray.warp(img, ow, max(1, round(ow / self.tray.aspect))))
            return cv2.imencode(".jpg", out, [cv2.IMWRITE_JPEG_QUALITY, 88])[1].tobytes()
        except Exception:
            return data

    def _cam_keep(self):
        """KITCHECK_CAMERA_KEEPALIVE=1: hold one live stream open all the time. go2rtc stops the camera when nobody
        watches and needs ~8-10 s to start it again; kept warm, new viewers attach at once and snapshots come
        from the latest frame here."""
        while True:
            try:
                conn, r = self._cam_get("/stream", 20)
                try:
                    reader = _FirstFrame(r)
                    while True:
                        data = reader.read()
                        self.cam_last = (time.monotonic(), data)
                finally:
                    conn.close()
            except (Err, OSError, HTTPException, ValueError):
                pass
            time.sleep(3)

    def _cam_get(self, path, timeout):
        if self.virtual:
            if path == "/snapshot":
                # newest frame; if /sim went quiet, its last frame (the virtual tray does not change by itself)
                jpg = self.vcam.latest(timeout=min(timeout, 3)) or self.vcam.jpeg
                if not jpg:
                    raise Err(502, "ยังไม่มีภาพจากถาดจำลอง (เปิดหน้า /sim ค้างไว้)")
                return _Done(), _Bytes(jpg)
            return _Done(), _VirtualStream(self.vcam)
        if path == "/snapshot" and self.cam_paths["/snapshot"] == "stream":
            # go2rtc's /api/frame.jpeg can take several seconds; use a live-stream frame instead
            t, data = self.cam_last
            if data and time.monotonic() - t < 2:
                return _Done(), _Bytes(data)
            conn, r = self._cam_get("/stream", max(timeout, 15))
            return conn, _FirstFrame(r)
        if not (self.cam_host and self.cam_auth):
            raise Err(503, "ยังไม่ได้ตั้งค่ากล้อง (KITCHECK_CAMERA_URL)")
        if self.cam_tls:
            conn = HTTPSConnection(self.cam_host, self.cam_port, timeout=timeout)
        else:
            conn = HTTPConnection(self.cam_host, self.cam_port, timeout=timeout)
        try:
            conn.request("GET", self.cam_base + self.cam_paths.get(path, path),
                         headers={"Authorization": self.cam_auth})
            r = conn.getresponse()
        except (OSError, HTTPException):
            conn.close()
            raise Err(502, "ต่อกล้องไม่ได้")
        if r.status != 200:
            conn.close()
            raise Err(502, "กล้องตอบผิดพลาด (%d)" % r.status)
        return conn, r

    def camera_snapshot(self, req):
        self._user(req)
        conn, r = self._cam_get("/snapshot", 6)
        try:
            data = r.read()
        except (OSError, HTTPException):
            raise Err(502, "อ่านภาพจากกล้องไม่ได้")
        finally:
            conn.close()
        return 200, {"Content-Type": "image/jpeg", "Cache-Control": "no-store"}, data

    def _snap_later(self):
        """Grab a camera frame in the background at about the moment CiRA's frame for this round is taken
        (LIVE_SETTLE_S after the request). Returns a function that waits for it and gives the JPEG or None."""
        if not (self.cam_host and self.cam_auth):
            return lambda: None
        from .mqtt_bridge import LIVE_SETTLE_S
        got = {}

        def run():
            time.sleep(LIVE_SETTLE_S)
            try:
                conn, r = self._cam_get("/snapshot", 5)
                try:
                    got["jpg"] = r.read()
                finally:
                    conn.close()
            except (Err, OSError, HTTPException):
                pass

        t = threading.Thread(target=run, daemon=True)
        t.start()

        def result():
            t.join(6)
            return got.get("jpg")
        return result

    def _keep_image(self, sid, data):
        """Save the round's photo as evidence (never overwrites, never fails the scan)."""
        if not data or data[:3] != b"\xff\xd8\xff":
            return
        data = self._tray_photo(data)
        name = f"{sid}.jpg"
        path = os.path.join(self.img_dir, name)
        if os.path.exists(path):
            return
        try:
            with open(path, "wb") as f:
                f.write(data)
        except OSError:
            return
        (self.desk if sid.startswith("ds_") else self.store).attach_image(sid, name)

    def camera_stream(self, headers, query, small=False):
        """For main.py: (status, headers, body) where body is a frame generator on success.
        small=True (the ESP32 kiosk, ?w=&h=&q=&fps=): each part is the tray-only picture from _small_frame with
        CiRA's boxes in an X-Detections part header, so the screen gets live video over one open request."""
        try:
            self._user({"headers": headers, "query": query})
            shrink = None
            if small:
                shrink = self._small_args(query)
                try:
                    fps = min(30.0, max(1.0, float(query.get("fps", 6))))  # ustreamer: cam-fps.sh sets 30
                except ValueError:
                    raise Err(400, "fps ไม่ถูกต้อง")
            sid = self._cam_take()
            if not sid:
                raise Err(503, "มีคนดูกล้องเต็มจำนวนแล้ว ลองใหม่ภายหลัง")
            try:
                conn, r = self._cam_get("/stream", 8)
            except Err:
                self._cam_drop(sid)
                raise
        except Err as e:
            return self._json(e.status, {"error": e.msg})
        return 200, {"Content-Type": "multipart/x-mixed-replace; boundary=frame", "Cache-Control": "no-store",
                     "X-Accel-Buffering": "no"}, self._frames(sid, conn, r, shrink, fps if small else None)

    def _cam_take(self):
        """Reserve a viewer slot. A stream that sent nothing for 5 s counts as gone (its client left), so a
        stream whose generator is closed late can never lock viewers out."""
        with self.cam_lock:
            now = time.monotonic()
            for k, t in list(self.cam_streams.items()):
                if now - t > 5:
                    del self.cam_streams[k]
            if len(self.cam_streams) >= self.cam_max:
                return None
            sid = secrets.token_hex(4)
            self.cam_streams[sid] = now
            return sid

    def _cam_drop(self, sid):
        with self.cam_lock:
            self.cam_streams.pop(sid, None)

    def _frames(self, sid, conn, r, shrink=None, fps=None):
        """Re-emit ustreamer's MJPEG parts, dropping frames above KITCHECK_CAMERA_FPS (saves upload bandwidth);
        with shrink=(w, h, q) each frame is cut to the tray and downscaled first."""
        min_dt, last = 1.0 / (fps or self.cam_fps), 0.0
        try:
            while True:
                line = r.readline(512)
                if not line:
                    return
                if not line.startswith(b"--"):
                    continue
                length = None
                while True:
                    h = r.readline(2048)
                    if not h:
                        return
                    h = h.strip()
                    if not h:
                        break
                    k, _, v = h.partition(b":")
                    if k.strip().lower() == b"content-length":
                        length = int(v.strip())
                if not length or length > 8 * 1024 * 1024:
                    return
                data = r.read(length)
                if len(data) < length:
                    return
                # perf_counter: time.monotonic() ticks in ~15.6 ms steps on Windows. 25% slack, and no limit at
                # all at >= 29 fps, so frames that arrive a little early (camera/driver jitter) are not dropped
                # (that turned 30 fps into ~18).
                now = time.monotonic()
                tick = time.perf_counter()
                if min_dt > 1 / 29 and tick - last < min_dt * 0.75:
                    continue
                last = tick
                with self.cam_lock:
                    self.cam_streams[sid] = now
                extra = b""
                if shrink:
                    data, dets = self._small_frame(data, *shrink)
                    extra = b"X-Detections: " + dets.encode("ascii") + b"\r\n"
                yield (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n" % len(data) + extra + b"\r\n"
                       + data + b"\r\n")
        except (OSError, HTTPException, ValueError):
            return
        finally:
            conn.close()
            self._cam_drop(sid)

    # ---------- auth ----------
    def login(self, req):
        b = self._body(req)
        token, user = self.store.login(b.get("username"), b.get("password"))
        return self._json(200, {"token": token, "user": user})

    def logout(self, req):
        self.store.logout(self._token(req))
        return self._json(200, {"ok": True})

    def get_state(self, req):
        return self._json(200, self.store.state(self._user(req)))

    def users(self, req):
        u = self._user(req)
        if u["role"] != "admin":
            raise Err(403, "เฉพาะผู้ดูแลระบบ")
        return self._json(200, {"users": self.store.list_users()})

    def create_user(self, req):
        u = self._user(req)
        if u["role"] != "admin":
            raise Err(403, "เฉพาะผู้ดูแลระบบ")
        b = self._body(req)
        self.store.create_user(b.get("username"), b.get("name"), b.get("password"), b.get("role", "staff"))
        return self._json(200, {"ok": True, "users": self.store.list_users()})

    # ---------- scans ----------
    def start_scan(self, req):
        u = self._user(req)
        b = self._body(req)
        kind = b.get("kind")
        sid = self.store.start_scan(u, b.get("kit"), kind, b.get("loan"), b.get("meta"))
        if self.mode == "mqtt":
            photo = self._snap_later()
            got = self.bridge.request(sid, b.get("kit"), kind, self.store_timeout())
            if not got:
                self.store.fail_scan(sid, "ไม่ได้รับผลจากจุดตรวจภายในเวลาที่กำหนด")
                return self._json(504, {"error": "ไม่ได้รับผลจากจุดตรวจภายใน %d วินาที ลองตรวจซ้ำ" % self.store_timeout(), "scan_id": sid})
            self._keep_image(sid, photo())
        else:
            self.store.simulate(sid, b.get("scenario", "ok"))
        return self._ok_state(u, {"scan_id": sid})

    def store_timeout(self):
        from .core import SCAN_TIMEOUT_S
        return SCAN_TIMEOUT_S

    def add_edit(self, req):
        u = self._user(req)
        b = self._body(req)
        self.store.add_edit(u, req["args"][0], b.get("type"), b.get("to"), b.get("reason"))
        return self._ok_state(u)

    def cancel_scan(self, req):
        u = self._user(req)
        self.store.cancel_scan(u, req["args"][0])
        return self._ok_state(u)

    def station_result(self, req):
        """HTTP alternative to MQTT scan/result, for a station that cannot speak MQTT."""
        self._station(req)
        b = self._body(req)
        sid = req["args"][0]
        target = self.desk if sid.startswith("ds_") else self.store
        ok = target.store_result(sid, b.get("detections", []), b.get("duration"))
        if ok and self.bridge:
            self.bridge.notify(req["args"][0])
        return self._json(200 if ok else 409, {"ok": ok})

    def station_image(self, req):
        self._station(req)
        sid = req["args"][0]
        data = req["body"] or b""
        if not data or len(data) > 15 * 1024 * 1024:
            raise Err(400, "ไฟล์ภาพว่างหรือใหญ่เกิน 15 MB")
        if not (data[:3] == b"\xff\xd8\xff" or data[:8] == b"\x89PNG\r\n\x1a\n"):
            raise Err(400, "รองรับเฉพาะ JPEG หรือ PNG")
        ext = "jpg" if data[:3] == b"\xff\xd8\xff" else "png"
        name = f"{sid}.{ext}"
        path = os.path.join(self.img_dir, name)
        if os.path.exists(path):
            raise Err(409, "ภาพของรอบนี้มีอยู่แล้ว ไม่เขียนทับ")
        with open(path, "wb") as f:
            f.write(data)
        (self.desk if sid.startswith("ds_") else self.store).attach_image(sid, name)
        return self._json(200, {"ok": True})

    def get_image(self, req):
        self._user(req)
        sid = req["args"][0]
        for ext, ct in (("jpg", "image/jpeg"), ("png", "image/png")):
            p = os.path.join(self.img_dir, f"{sid}.{ext}")
            if os.path.exists(p):
                with open(p, "rb") as f:
                    return 200, {"Content-Type": ct, "Cache-Control": "private, max-age=86400"}, f.read()
        raise Err(404, "ไม่มีภาพ")

    # ---------- v2: kiosk (self-service) ----------
    def kiosk_page(self, req):
        with open(os.path.join(STATIC, "kiosk.html"), "rb") as f:
            return 200, {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-cache"}, f.read()

    def kiosk_state(self, req):
        self._user(req)
        out = self.desk.kiosk_state()
        out["mode"] = self.mode
        out["mqtt"] = self.store.mqtt_connected
        out.update(self.camera_info())
        return self._json(200, out)

    def kiosk_member(self, req):
        self._user(req)
        return self._json(200, {"member": self.desk.find_member(self._body(req).get("card"))})

    def kiosk_scan(self, req):
        u = self._user(req)
        b = self._body(req)
        sid = self.desk.start_scan(u, b.get("kind"), b.get("member_id"))
        if self.mode == "mqtt":
            from .core import SCAN_TIMEOUT_S
            photo = self._snap_later()
            if not self.bridge.request(sid, None, b.get("kind"), SCAN_TIMEOUT_S):
                self.desk.fail_scan(sid, "ไม่ได้รับผลจากจุดตรวจ")
                return self._json(504, {"error": "กล้องไม่ตอบภายใน %d วินาที ลองใหม่อีกครั้ง" % SCAN_TIMEOUT_S, "scan_id": sid})
            self._keep_image(sid, photo())
        else:
            self.desk.simulate(sid)
        return self._json(200, {"scan": self.desk.scan_view(sid)})

    def kiosk_scan_get(self, req):
        self._user(req)
        return self._json(200, {"scan": self.desk.scan_view(req["args"][0])})

    def kiosk_confirm(self, req):
        u = self._user(req)
        self.desk.confirm(u, req["args"][0])
        return self._json(200, {"scan": self.desk.scan_view(req["args"][0])})

    def kiosk_cancel(self, req):
        u = self._user(req)
        self.desk.cancel_scan(u, req["args"][0])
        return self._json(200, {"ok": True})

    def sim_tray(self, req):
        self._user(req)
        b = self._body(req)
        self.desk.set_sim_tray(b.get("qrs") or [], b.get("unread") or [])
        return self._json(200, self.desk.kiosk_state())

    # ---------- v2: staff ----------
    def desk_state(self, req):
        out = self.desk.state(self._user(req))
        out["camera"] = self.camera_info()
        return self._json(200, out)

    def _desk_ok(self, user, extra=None):
        out = {"ok": True, "state": self.desk.state(user)}
        if extra:
            out.update(extra)
        return self._json(200, out)

    def tool_create(self, req):
        u = self._user(req)
        b = self._body(req)
        codes = self.desk.create_tools(u, b.get("type"), b.get("weight_g"), b.get("count", 1), b.get("note", ""))
        return self._desk_ok(u, {"codes": codes})

    def tool_update(self, req):
        u = self._user(req)
        b = self._body(req)
        self.desk.update_tool(u, req["args"][0], b.get("weight_g"), b.get("active"), b.get("note"))
        return self._desk_ok(u)

    def tool_reissue(self, req):
        u = self._user(req)
        return self._desk_ok(u, {"qr": self.desk.reissue_tool(u, req["args"][0])})

    def member_create(self, req):
        u = self._user(req)
        b = self._body(req)
        self.desk.create_member(u, b.get("code"), b.get("name"), b.get("phone", ""), b.get("card"))
        return self._desk_ok(u)

    def member_update(self, req):
        u = self._user(req)
        b = self._body(req)
        self.desk.update_member(u, req["args"][0], b.get("name"), b.get("phone"), b.get("card"), b.get("active"))
        return self._desk_ok(u)

    def desk_resolve(self, req):
        u = self._user(req)
        b = self._body(req)
        self.desk.resolve(u, req["args"][0], b.get("assign") or {}, b.get("note", ""))
        return self._desk_ok(u)

    def desk_lost(self, req):
        u = self._user(req)
        self.desk.mark_lost(u, req["args"][0], self._body(req).get("note", ""))
        return self._desk_ok(u)

    def station_scale(self, req):
        """Load-cell reading from the tray (ESP32), HTTP alternative to MQTT <prefix>/scale."""
        self._station(req)
        b = self._body(req)
        self.desk.set_scale(b.get("grams"), b.get("stable"))
        return self._json(200, {"ok": True})

    # ---------- transactions ----------
    def borrow(self, req):
        u = self._user(req)
        b = self._body(req)
        loan = self.store.confirm_borrow(u, b.get("scan_id"), b.get("who"), b.get("phone"), b.get("days"))
        return self._ok_state(u, {"loan_id": loan})

    def do_return(self, req):
        u = self._user(req)
        b = self._body(req)
        self.store.confirm_return(u, b.get("scan_id"), b.get("loan_id"))
        return self._ok_state(u)

    def anomaly(self, req):
        u = self._user(req)
        b = self._body(req)
        self.store.record_anomaly(u, b.get("scan_id"), b.get("loan_id"), b.get("note", ""))
        return self._ok_state(u)

    def settle(self, req):
        u = self._user(req)
        b = self._body(req)
        self.store.close_with_deduction(u, req["args"][0], b.get("deduct", 0), b.get("reason"))
        return self._ok_state(u)

    # ---------- kits / items ----------
    def create_kit(self, req):
        u = self._user(req)
        b = self._body(req)
        n = max(1, min(50, int(b.get("count", 1))))
        ids = [self.store.create_kit(u, b.get("name"), b.get("deposit", 0), b.get("master")) for _ in range(n)]
        return self._ok_state(u, {"kit_ids": ids})

    def update_kit(self, req):
        u = self._user(req)
        b = self._body(req)
        self.store.update_kit(u, req["args"][0], b.get("status"), b.get("name"), b.get("deposit"))
        return self._ok_state(u)

    def reissue(self, req):
        u = self._user(req)
        new = self.store.reissue_item(u, req["args"][0])
        return self._ok_state(u, {"qr": new})

    def qr_svg(self, req):
        try:
            import qrcode
            import qrcode.image.svg
        except ImportError:
            raise Err(501, "ยังไม่ได้ติดตั้งแพ็กเกจ qrcode (pip install qrcode)")
        img = qrcode.make(req["args"][0], image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=2)
        buf = io.BytesIO()
        img.save(buf)
        return 200, {"Content-Type": "image/svg+xml", "Cache-Control": "public, max-age=86400"}, buf.getvalue()

    def labels(self, req):
        u = self._user(req)
        kit_id = req["args"][0]
        st = self.store.state(u)
        kit = next((k for k in st["kits"] if k["id"] == kit_id), None)
        if not kit:
            raise Err(404, "ไม่พบชุด")
        names = {t["key"]: t["name"] for t in st["types"]}
        cells = "".join(
            f'<div class="lab"><img src="/api/qr/{html.escape(q)}.svg" alt=""><div><b>{html.escape(q)}</b><br>'
            f'{html.escape(names.get(st["items"][q]["type"], ""))}<br><small>{html.escape(kit_id)}</small></div></div>'
            for q in kit["items"])
        page = f"""<!DOCTYPE html><html lang="th"><head><meta charset="utf-8"><title>ป้าย QR {html.escape(kit_id)}</title>
<style>body{{font-family:'IBM Plex Sans Thai',system-ui,sans-serif;margin:16px}}.grid{{display:flex;flex-wrap:wrap;gap:6mm}}
.lab{{width:60mm;height:28mm;border:1px dashed #999;display:flex;align-items:center;gap:3mm;padding:2mm;box-sizing:border-box;font-size:10pt;line-height:1.3}}
.lab img{{width:24mm;height:24mm}}@media print{{.no-print{{display:none}}.lab{{border-color:#ddd}}}}</style></head><body>
<div class="no-print"><h1 style="font-size:18px">ป้าย QR · {html.escape(kit_id)} · {html.escape(kit['name'])}</h1>
<p>พิมพ์บนสติกเกอร์แบบลอกแล้วเสีย แล้วติดบนเครื่องมือแต่ละชิ้นตามชนิด <button onclick="window.print()">พิมพ์</button></p></div>
<div class="grid">{cells}</div></body></html>"""
        return 200, {"Content-Type": "text/html; charset=utf-8"}, page.encode("utf-8")


_api = None


def get_api():
    global _api
    if _api is None:
        _api = Api()
    return _api
