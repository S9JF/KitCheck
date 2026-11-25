"""MQTT link between the server and the CiRA CORE checkpoint.

Topics (prefix defaults to "kitcheck"):
  <prefix>/scan/request  server -> station  {"scan_id","kit_id","kind","expires_at"}
  <prefix>/scan/result   station -> server  {"scan_id","detections":[{"type","conf","qr","box":[x,y,w,h]}],"duration"}
  <prefix>/status        station -> server  {"station","online","ready",...}
  <prefix>/scale         tray ESP32 -> server  {"grams": 1006.4, "stable": true}
  <prefix>/station/live  CiRA CORE -> server  {"detections":[{"type","conf","qr","box":[x,y,w,h]}]}
                         published continuously (every processed frame). A scan round takes the first
                         live message that arrives after the round started, so the CiRA flow needs no
                         trigger logic: camera -> Yolo11 -> QR reader -> format -> MQTT publish.
Kiosk rounds (v2) use scan ids "ds_00001"; kit rounds keep "scan_0001".

Commands are never retained, so a station that comes back online does not replay old requests.
A result whose scan_id is not the current waiting round is ignored by Store.store_result.
"""
import json
import secrets
import threading
import time

from .core import Err

LIVE_SETTLE_S = 0.6
MAX_BOX_AREA = 0.35  # of the frame; the largest tool (a drill) covers well under this
MAX_TRAY_BOX_AREA = 0.6  # of the tray picture (tools look bigger there): only "the whole tray is a tool" is dropped
SAME_TOOL_OVERLAP = 0.8  # two boxes sharing this much of the smaller one are one tool seen twice


def dedupe(dets):
    """YOLO suppresses overlapping boxes only within one class, so one tool can come back twice under two names
    (pliers 0.80 + "wrench" 0.56 on the same spot) and the round would count a phantom piece with an unread QR.
    Keep, per spot, the box with a read QR first, then the most confident one."""
    def box(d):
        try:
            x, y, w, h = (float(v) for v in d["box"][:4])
            return x, y, w, h
        except (KeyError, TypeError, ValueError):
            return None

    order = sorted(dets, key=lambda d: (not (isinstance(d, dict) and d.get("qr")),
                                        -float(d.get("conf") or 0) if isinstance(d, dict) else 0))
    kept = []
    for d in order:
        b = box(d) if isinstance(d, dict) else None
        dup = False
        if b and b[2] > 0 and b[3] > 0:
            for k in kept:
                kb = box(k)
                if not kb or (d.get("qr") and k.get("qr") and d["qr"] != k["qr"]):
                    continue  # two different QR codes = two tools, however close
                iw = min(b[0] + b[2], kb[0] + kb[2]) - max(b[0], kb[0])
                ih = min(b[1] + b[3], kb[1] + kb[3]) - max(b[1], kb[1])
                if iw > 0 and ih > 0 and iw * ih >= SAME_TOOL_OVERLAP * min(b[2] * b[3], kb[2] * kb[3]):
                    dup = True
                    break
        if not dup:
            kept.append(d)
    return [d for d in dets if any(d is k for k in kept)]  # original order

class Bridge:
    def __init__(self, store, host, port, prefix, username=None, password=None, desk=None, crop=None, tray=None):
        import paho.mqtt.client as mqtt

        self.store = store
        self.crop = crop  # (x0,y0,x1,y1) of the tray in the frame; detections centred outside it are dropped
        self.tray = tray  # tray.Tray: when its corners are set, boxes are mapped onto the straightened tray
        self.desk = desk
        self.prefix = prefix.rstrip("/")
        self.events = {}
        self.live_waiters = {}  # scan_id -> earliest time (s) a live frame may be used
        self.last_live = 0.0
        self.live = None  # latest station/live payload {"detections","at"} for the browser's live overlay
        self.lock = threading.Lock()
        cid = "kitcheck-server-" + secrets.token_hex(3)
        try:
            self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=cid)
        except AttributeError:  # paho-mqtt < 2.0
            self.client = mqtt.Client(client_id=cid)
        if username:
            self.client.username_pw_set(username, password or None)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message
        self.client.reconnect_delay_set(min_delay=1, max_delay=10)
        self.client.connect_async(host, port, keepalive=30)
        self.client.loop_start()

    def _on_connect(self, client, userdata, *args):
        self.store.mqtt_connected = True
        client.subscribe(self.prefix + "/scan/result", qos=1)
        client.subscribe(self.prefix + "/status", qos=1)
        client.subscribe(self.prefix + "/scale", qos=0)
        client.subscribe(self.prefix + "/station/live", qos=0)

    def _on_disconnect(self, *args):
        self.store.mqtt_connected = False

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        if not isinstance(payload, dict):
            return
        if isinstance(payload.get("detections"), list):
            payload["detections"] = self.on_tray(payload["detections"])
        if msg.topic.endswith("/scan/result"):
            sid = str(payload.get("scan_id", ""))
            target = self.desk if (self.desk and sid.startswith("ds_")) else self.store
            if target.store_result(sid, payload.get("detections") or [], payload.get("duration")):
                self.notify(sid)
        elif msg.topic.endswith("/station/live"):
            self._live(payload)
        elif msg.topic.endswith("/status"):
            self.store.set_station(payload)
        elif msg.topic.endswith("/scale") and self.desk and not getattr(self.desk, "virtual", False):
            # (with virtual hardware the /sim page is the scale; the real one is ignored)
            try:
                self.desk.set_scale(payload.get("grams"), payload.get("stable"))
            except Err:
                pass

    def on_live(self, payload):
        """A CiRA live result that came over HTTP (CiRA on the same PC) instead of MQTT."""
        if isinstance(payload.get("detections"), list):
            payload["detections"] = self.on_tray(payload["detections"])
        self._live(payload)

    def _live(self, payload):
        now = time.time()
        self.last_live = now
        if "detections" not in payload:  # CiRA idle frame (no scan waiting): not a result
            return
        dets = payload.get("detections")
        ts = payload.get("ts") if isinstance(payload.get("ts"), (int, float)) else None
        if ts and self.live and self.live.get("ts") and ts <= self.live["ts"]:
            return  # overtaken by a newer frame's result (results can arrive out of order over a long link)
        self.live = {"detections": dets if isinstance(dets, list) else [], "at": now, "ts": ts,
                     "qr_read": payload.get("qr_read")}  # did CiRA try to read QR codes on this frame?
        with self.lock:
            due = [sid for sid, after in self.live_waiters.items() if now >= after]
        for sid in due:
            target = self.desk if (self.desk and sid.startswith("ds_")) else self.store
            if target.store_result(sid, payload.get("detections") or [], payload.get("duration")):
                self.notify(sid)

    def on_tray(self, dets):
        """Keep detections whose box centre is on the tray (things lying next to it are not part of the round),
        and drop boxes too big to be one tool (the model sometimes calls the whole empty frame a tool).
        With the tray's corners set, the kept boxes come back as 0..1 of the straightened tray picture."""
        if self.tray is not None and self.tray.active:
            out = []
            for d in dets:
                b = d.get("box") if isinstance(d, dict) else None
                try:
                    tb = self.tray.map_box(b)
                except (TypeError, ValueError, IndexError):
                    continue
                if tb is None or tb[2] * tb[3] > MAX_TRAY_BOX_AREA:
                    continue
                out.append(dict(d, box=tb))
            return dedupe(out)
        x0, y0, x1, y1 = self.crop or (0.0, 0.0, 1.0, 1.0)
        out = []
        for d in dets:
            b = d.get("box") if isinstance(d, dict) else None
            try:
                bw, bh = float(b[2]), float(b[3])
                cx, cy = float(b[0]) + bw / 2, float(b[1]) + bh / 2
            except (TypeError, ValueError, IndexError):
                out.append(d)  # no usable box: let the round's own checks decide
                continue
            if bw * bh > MAX_BOX_AREA:
                continue
            if x0 <= cx <= x1 and y0 <= cy <= y1:
                out.append(d)
        return dedupe(out)

    def notify(self, scan_id):
        with self.lock:
            ev = self.events.get(scan_id)
        if ev:
            ev.set()

    def request(self, scan_id, kit_id, kind, timeout):
        if not self.store.mqtt_connected:
            raise Err(503, "ยังเชื่อมต่อ MQTT Broker ไม่ได้ ตรวจว่า broker ทำงานอยู่")
        ev = threading.Event()
        with self.lock:
            self.events[scan_id] = ev
            # skip frames already in flight: use one captured at least LIVE_SETTLE_S after the request
            self.live_waiters[scan_id] = time.time() + LIVE_SETTLE_S
        try:
            body = {"scan_id": scan_id, "kit_id": kit_id, "kind": kind, "expires_at": int(time.time()) + timeout}
            self.client.publish(self.prefix + "/scan/request", json.dumps(body), qos=1, retain=False)
            return ev.wait(timeout)
        finally:
            with self.lock:
                self.events.pop(scan_id, None)
                self.live_waiters.pop(scan_id, None)

    def publish_event(self, ev):
        """<prefix>/event: one KitCheck event (borrow, return, round stopped, staff action) for CiRA CORE's
        MqttSub node -> LineBot / TGNotify. 'message' is ready-to-send text."""
        when = time.strftime("%d/%m %H:%M", time.localtime(ev["ts"] / 1000))
        icon = {"ยืม": "📦", "คืน": "✅", "ผิดปกติ": "⚠️", "รอตรวจ": "🔎"}.get(ev.get("tag"), "ℹ️")
        msg = "%s KitCheck · %s\n%s\n%s" % (icon, ev["what"], ev.get("sub") or "", when)
        body = dict(ev, message=msg.replace("\n\n", "\n"))
        try:
            self.client.publish(self.prefix + "/event", json.dumps(body, ensure_ascii=False), qos=1, retain=False)
        except Exception:
            pass

    def publish_sim_scale(self, grams, stable):
        """<prefix>/sim/scale: the /sim page's virtual scale, so CiRA can show/use it like a real scale."""
        try:
            self.client.publish(self.prefix + "/sim/scale", json.dumps({"grams": grams, "stable": bool(stable)}),
                                qos=0, retain=False)
        except Exception:
            pass

    def wanted(self):
        """True while a scan is waiting for CiRA's next live result (CiRA runs YOLO only then)."""
        with self.lock:
            return bool(self.live_waiters)

    def live_age(self):
        """Seconds since CiRA last published a live result (None = never)."""
        return None if not self.last_live else round(time.time() - self.last_live, 1)
