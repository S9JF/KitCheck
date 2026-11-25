"""USB camera on this PC -> a small ustreamer-compatible MJPEG server (GET /snapshot, GET /stream) on 127.0.0.1.

KitCheck reads the camera exactly as it reads ustreamer on the Linux server (KITCHECK_CAMERA_URL + Basic auth),
so api.py needs no change. ffmpeg grabs the camera through DirectShow and passes the camera's own MJPEG frames
through without re-encoding (almost no CPU). The camera only runs while someone is watching (+15 s).

    KITCHECK_LOCAL_CAMERA   camera name as Windows lists it (default: the first one that is not a built-in
                            "User Facing"/"Integrated"/IR camera, else the first camera)
    KITCHECK_LOCAL_CAMERA_SIZE / _FPS   default 1280x720 / 30
"""
import os
import re
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

IDLE_STOP_S = 15
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def ffmpeg_path():
    p = shutil.which("ffmpeg")
    if not p:
        raise SystemExit("ffmpeg not found: install it with  winget install Gyan.FFmpeg")
    return p


def list_cameras():
    out = subprocess.run([ffmpeg_path(), "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
                         capture_output=True, text=True, errors="replace", creationflags=NO_WINDOW).stderr
    return re.findall(r'"([^"]+)" \(video\)', out)


def pick_camera():
    want = os.environ.get("KITCHECK_LOCAL_CAMERA", "").strip()
    cams = list_cameras()
    if want:
        for c in cams:
            if c.lower() == want.lower():
                return c, cams
        for c in cams:
            if want.lower() in c.lower():
                return c, cams
        return None, cams
    builtin = re.compile(r"user facing|integrated|\bIR\b|virtual", re.I)
    for c in cams:
        if not builtin.search(c):
            return c, cams
    return (cams[0] if cams else None), cams


class Camera:
    def __init__(self, name, size="1280x720", fps=30):
        self.name, self.size, self.fps = name, size, fps
        self.cond = threading.Condition()
        self.jpeg, self.seq, self.used, self.at = None, 0, 0.0, -1e9
        self.copy = True  # pass the camera's MJPEG through; falls back to encoding if the camera has no MJPEG
        self.error = ""
        threading.Thread(target=self._run, name="localcam", daemon=True).start()

    def touch(self):
        with self.cond:
            self.used = time.monotonic()
            self.cond.notify_all()

    def latest(self, after_seq, timeout):
        """Wait for a frame newer than after_seq; returns (seq, jpeg) or (after_seq, None) on timeout."""
        end = time.monotonic() + timeout
        with self.cond:
            self.used = time.monotonic()
            self.cond.notify_all()
            # Never hand out an old picture (e.g. the camera was unplugged): only frames under 2 s old.
            while self.seq <= after_seq or time.monotonic() - self.at > 2.0:
                left = end - time.monotonic()
                if left <= 0:
                    return after_seq, None
                self.cond.wait(left)
            return self.seq, self.jpeg

    def _cmd(self):
        cmd = [ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-f", "dshow", "-rtbufsize", "32M"]
        if self.copy:
            cmd += ["-vcodec", "mjpeg"]
        cmd += ["-video_size", self.size, "-framerate", str(self.fps), "-i", "video=" + self.name]
        cmd += ["-c:v", "copy"] if self.copy else ["-c:v", "mjpeg", "-q:v", "5", "-pix_fmt", "yuvj420p"]
        return cmd + ["-f", "mpjpeg", "-"]

    def _run(self):
        while True:
            with self.cond:
                while time.monotonic() - self.used > IDLE_STOP_S:
                    self.cond.wait(1.0)
            started, frames = time.monotonic(), 0
            p = subprocess.Popen(self._cmd(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 creationflags=NO_WINDOW, bufsize=0)
            try:
                frames = self._read(p)
            finally:
                p.kill()
                err = p.stderr.read().decode("utf-8", "replace").strip() if p.stderr else ""
                p.wait()
            if frames == 0:
                self.error = err.splitlines()[-1] if err else "camera gave no picture"
                print("[localcam] %s: %s" % (self.name, self.error), flush=True)
                if self.copy and time.monotonic() - started < 10:
                    self.copy = False  # no MJPEG mode at this size/rate: let ffmpeg encode instead
                    print("[localcam] retrying with ffmpeg encoding", flush=True)
                    continue
                self.copy = True
                time.sleep(3)

    def _read(self, p):
        """Parse ffmpeg's multipart output; stops when nobody watched for IDLE_STOP_S."""
        r, frames = p.stdout, 0
        while True:
            line = r.readline(512)
            if not line:
                return frames
            if not line.startswith(b"--"):
                continue
            length = None
            while True:
                h = r.readline(2048)
                if not h:
                    return frames
                h = h.strip()
                if not h:
                    break
                k, _, v = h.partition(b":")
                if k.strip().lower() == b"content-length":
                    length = int(v.strip())
            if not length:
                return frames
            data = b""
            while len(data) < length:
                chunk = r.read(length - len(data))
                if not chunk:
                    return frames
                data += chunk
            frames += 1
            self.error = ""
            with self.cond:
                self.jpeg, self.seq, self.at = data, self.seq + 1, time.monotonic()
                self.cond.notify_all()
                if time.monotonic() - self.used > IDLE_STOP_S:
                    return frames


def serve(camera, auth, host="127.0.0.1", port=8081):
    """auth: the full expected Authorization header value (Basic ...), or None for no password (the relay for
    CiRA on this PC: it only listens on 127.0.0.1)."""

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _deny(self, code, msg):
            body = msg.encode()
            self.send_response(code)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if auth and self.headers.get("Authorization") != auth:
                return self._deny(401, "unauthorized")
            path = self.path.split("?")[0]
            if path == "/snapshot":
                _, jpg = camera.latest(0, 6)
                if not jpg:
                    return self._deny(503, camera.error or "no picture")
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(jpg)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(jpg)
            elif path == "/stream":
                self.send_response(200)
                self.send_header("Content-Type", "multipart/x-mixed-replace;boundary=boundarydonotcross")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.end_headers()
                # ?fps=N: send at most N frames a second (a far-away viewer such as the cloud GPU's CiRA saves upload)
                try:
                    q = dict(p.split("=", 1) for p in self.path.partition("?")[2].split("&") if "=" in p)
                    min_dt = 1.0 / max(0.5, float(q["fps"])) if "fps" in q else 0.0
                except ValueError:
                    min_dt = 0.0
                seq, last = 0, 0.0
                try:
                    while True:
                        seq, jpg = camera.latest(seq, 8)
                        if not jpg:
                            return
                        now = time.perf_counter()
                        if min_dt and now - last < min_dt * 0.9:
                            continue
                        last = now
                        self.wfile.write(b"--boundarydonotcross\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n"
                                         % len(jpg) + jpg + b"\r\n")
                except OSError:
                    return
            else:
                self._deny(404, "not found")

    srv = ThreadingHTTPServer((host, port), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, name="localcam-http", daemon=True).start()
    return srv
