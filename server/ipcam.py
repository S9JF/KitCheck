"""The USB camera on this PC as an IP camera (ustreamer-compatible: GET /stream = MJPEG, GET /snapshot = JPEG,
HTTP Basic auth user "camera"). KitCheck on any server reads it with
    KITCHECK_CAMERA_URL=http://<this PC's Tailscale IP>:8081
    KITCHECK_CAMERA_USER=camera
    KITCHECK_CAMERA_PASS=<IPCAM_PASS from ipcam.env>

    .venv\\Scripts\\python.exe -I ipcam.py

Settings in ipcam.env next to this file (created on first run with a random password, which is never printed):
    IPCAM_PASS     the password
    IPCAM_PORT     default 8081
    IPCAM_BIND     default 0.0.0.0 (every network); e.g. this PC's Tailscale IP to serve only over Tailscale
    KITCHECK_LOCAL_CAMERA, KITCHECK_LOCAL_CAMERA_SIZE (1280x720), KITCHECK_LOCAL_CAMERA_FPS (30)
The camera only runs while someone is pulling the stream (+15 s).
"""
import os
import secrets
import socket
import sys
import time
from base64 import b64encode

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import localcam  # noqa: E402

ENV = os.path.join(HERE, "ipcam.env")


def load_env():
    vals = {}
    if os.path.exists(ENV):
        with open(ENV, encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    vals[k.strip()] = v.strip()
    if not vals.get("IPCAM_PASS"):
        vals["IPCAM_PASS"] = secrets.token_urlsafe(18)
        with open(ENV, "a", encoding="utf-8") as f:
            f.write("# IP camera password (user: camera). Give it to the server's KITCHECK_CAMERA_PASS.\n"
                    "IPCAM_PASS=%s\n" % vals["IPCAM_PASS"])
        print("new password written to", ENV)
    for k, v in vals.items():
        os.environ.setdefault(k, v)


def main():
    load_env()
    env = os.environ
    want = env.get("KITCHECK_LOCAL_CAMERA", "").strip()
    name, cams = localcam.pick_camera()
    if want and not name:
        name = want  # named camera not plugged in (yet): keep trying it, never fall back to another camera
        print("waiting for camera %r (found now: %s)" % (want, ", ".join(cams) or "none"))
    if not name:
        sys.exit("no camera found (found: %s)" % (", ".join(cams) or "none"))
    size = env.get("KITCHECK_LOCAL_CAMERA_SIZE", "1280x720")
    fps = int(env.get("KITCHECK_LOCAL_CAMERA_FPS", "30"))
    port = int(env.get("IPCAM_PORT", "8081"))
    bind = env.get("IPCAM_BIND", "0.0.0.0")
    cam = localcam.Camera(name, size, fps)
    auth = "Basic " + b64encode(("camera:" + env["IPCAM_PASS"]).encode()).decode()
    localcam.serve(cam, auth, host=bind, port=port)
    # Loopback without a password, for KitCheck/CiRA on this same PC (run_local.py, CiRA GstCam).
    if bind not in ("127.0.0.1",):
        try:
            localcam.serve(cam, None, host="127.0.0.1", port=int(env.get("IPCAM_LOCAL_PORT", "8082")))
        except OSError:
            pass
    print("IP camera: %s (%s @ %d fps)" % (name, size, fps))
    ips = set()
    try:
        ips = {i[4][0] for i in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)}
    except OSError:
        pass
    for ip in sorted(i for i in ips if not i.startswith(("127.", "169.254."))):
        tag = "  (Tailscale)" if ip.startswith("100.") else ""
        print("  http://%s:%d/stream   /snapshot   user: camera%s" % (ip, port, tag))
    print("  this PC without password: http://127.0.0.1:%s/stream" % env.get("IPCAM_LOCAL_PORT", "8082"))
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
