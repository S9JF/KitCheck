"""KitCheck on this PC: web + kiosk API on port 8080, the camera plugged into this PC, and the MQTT broker on
the saer server (tray scale). Data lives in local_data\\ next to this file.

    .venv\\Scripts\\python.exe -I run_local.py

Settings come from local.env (KEY=VALUE per line, next to this file); anything not set there uses the defaults
below. Put the MQTT user and password in local.env yourself; this script never prints them.
"""
import os
import secrets
import socket
import sys
from base64 import b64encode

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def load_env(path):
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            os.environ.setdefault(k.strip(), v)


load_env(os.path.join(HERE, "local.env"))
env = os.environ
env.setdefault("KITCHECK_DATA", os.path.join(HERE, "local_data"))
env.setdefault("KITCHECK_MODE", "mqtt")  # AI: CiRA CORE on this PC posts results to /api/station/live
env.setdefault("KITCHECK_MQTT_ENABLE", "1")
env.setdefault("KITCHECK_MQTT_HOST", "100.124.5.71")  # saer over Tailscale
env.setdefault("KITCHECK_MQTT_PORT", "1883")
env.setdefault("KITCHECK_CAMERA_FPS", "30")
PORT = int(env.get("KITCHECK_PORT", "8080"))
CAM_PORT = int(env.get("KITCHECK_LOCAL_CAMERA_PORT", "8081"))
RELAY_PORT = int(env.get("KITCHECK_CIRA_CAMERA_PORT", "8082"))

if env["KITCHECK_MQTT_ENABLE"] == "1" and not (env.get("KITCHECK_MQTT_USER") and env.get("KITCHECK_MQTT_PASS")):
    print("! KITCHECK_MQTT_USER / KITCHECK_MQTT_PASS not set in local.env: the tray weight will not arrive")

# Camera: a private, per-run password between KitCheck and the local camera server.
import localcam  # noqa: E402

def ipcam_running():
    """ipcam.py (the USB camera as an IP camera) already owns the camera: read it from there."""
    path = os.path.join(HERE, "ipcam.env")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8-sig") as f:
        pw = next((l.split("=", 1)[1].strip() for l in f if l.startswith("IPCAM_PASS=")), None)
    try:
        socket.create_connection(("127.0.0.1", int(env.get("IPCAM_PORT", "8081"))), timeout=1).close()
    except OSError:
        return None
    return pw


remote_cam = env.get("KITCHECK_CAMERA_URL")  # set in local.env: an IP camera elsewhere (e.g. go2rtc over HTTPS)
ipcam_pass = None if remote_cam else ipcam_running()
cam_name, cams = (None, []) if (remote_cam or ipcam_pass) else localcam.pick_camera()
if env.get("KITCHECK_VIRTUAL") == "1":
    print("camera + scale: VIRTUAL (open /sim in a browser; the AI station pulls /api/virtual/stream)")
elif remote_cam:
    print("camera: IP camera from local.env at", remote_cam)
elif ipcam_pass:
    env["KITCHECK_CAMERA_URL"] = "http://127.0.0.1:%s" % env.get("IPCAM_PORT", "8081")
    env["KITCHECK_CAMERA_USER"] = "camera"
    env["KITCHECK_CAMERA_PASS"] = ipcam_pass
    print("camera: from ipcam.py (already running) at", env["KITCHECK_CAMERA_URL"])
elif cam_name:
    cam_pass = secrets.token_urlsafe(18)
    env["KITCHECK_CAMERA_URL"] = "http://127.0.0.1:%d" % CAM_PORT
    env["KITCHECK_CAMERA_USER"] = "camera"
    env["KITCHECK_CAMERA_PASS"] = cam_pass
    size = env.get("KITCHECK_LOCAL_CAMERA_SIZE", "1280x720")
    fps = int(env.get("KITCHECK_LOCAL_CAMERA_FPS", "30"))
    cam = localcam.Camera(cam_name, size, fps)
    localcam.serve(cam, "Basic " + b64encode(("camera:" + cam_pass).encode()).decode(), port=CAM_PORT)
    # Same picture for CiRA CORE on this PC (GstCam url http://127.0.0.1:8082/stream), so the two programs do
    # not fight over the camera. Loopback only, so no password.
    localcam.serve(cam, None, port=RELAY_PORT)
    print("camera: %s (%s @ %d fps)   [found: %s]" % (cam_name, size, fps, ", ".join(cams)))
    print("camera for CiRA: http://127.0.0.1:%d/stream" % RELAY_PORT)
else:
    env.pop("KITCHECK_CAMERA_URL", None)
    print("! no camera found%s   [found: %s]" % (
        " named " + repr(env["KITCHECK_LOCAL_CAMERA"]) if env.get("KITCHECK_LOCAL_CAMERA") else "",
        ", ".join(cams) or "none"))

import uvicorn  # noqa: E402
from kitcheck.main import app  # noqa: E402  (reads the settings above when imported)


def lan_ips():
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    return sorted(i for i in ips if not i.startswith(("127.", "169.254.")))


print("data: %s   mode: %s   mqtt: %s:%s" % (env["KITCHECK_DATA"], env["KITCHECK_MODE"],
                                            env["KITCHECK_MQTT_HOST"], env["KITCHECK_MQTT_PORT"]))
for ip in lan_ips() or ["127.0.0.1"]:
    print("open: http://%s:%d/   kiosk: http://%s:%d/kiosk" % (ip, PORT, ip, PORT))
uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
