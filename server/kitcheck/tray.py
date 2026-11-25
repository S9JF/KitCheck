"""The tray in the camera picture: its 4 corners, and the perspective warp that turns that (possibly slanted)
quadrilateral into a straight rectangle, whatever angle the camera looks from.

Corners are stored in <data>/tray.json as 0..1 of the camera frame, clockwise from top-left:
    {"quad": [[x,y] tl, tr, br, bl], "frame": [1280, 720], "aspect": 1.6}
The warp only runs per frame (a few ms); the corners are found once, when someone presses "find the tray"
(auto_quad), and can be dragged into place by hand before saving.
"""
import json
import os
import threading

try:
    import cv2
    import numpy as np
except ImportError:  # the warp needs OpenCV; without it KitCheck falls back to the plain rectangular crop
    cv2 = np = None


def order(pts):
    """4 points -> clockwise from top-left (tl, tr, br, bl)."""
    p = np.asarray(pts, dtype=np.float32).reshape(4, 2)
    c = p.mean(0)
    ang = np.arctan2(p[:, 1] - c[1], p[:, 0] - c[0])
    p = p[np.argsort(ang)]  # clockwise in image coords, starting somewhere
    start = int(np.argmin(p.sum(1)))  # top-left = smallest x+y
    return np.roll(p, -start, axis=0)


def auto_quad(img):
    """Best guess of the tray's corners in a BGR frame (0..1 of the frame), or None.
    The tray's outline is a strong edge against the floor, while light falling unevenly across the pad only makes
    smooth gradients (no edges). So: edges become walls, and the tray is the walled-in region (grown from several
    seeds) that is most like a dark quadrilateral lying inside the picture, covering 8-90 % of it."""
    h, w = img.shape[:2]
    s = 640.0 / w
    small = cv2.resize(img, (640, int(round(h * s))), interpolation=cv2.INTER_AREA)
    sh, sw = small.shape[:2]
    blur = cv2.bilateralFilter(small, 9, 50, 9)
    lab = cv2.cvtColor(blur, cv2.COLOR_BGR2LAB)
    walls = cv2.dilate(cv2.Canny(cv2.cvtColor(blur, cv2.COLOR_BGR2GRAY), 20, 60), np.ones((3, 3), np.uint8))
    n_lbl, lbl = cv2.connectedComponents((walls == 0).astype(np.uint8), connectivity=4)
    best, tried = None, set()
    for fy in (0.5, 0.4, 0.6, 0.3, 0.7):
        for fx in (0.5, 0.4, 0.6, 0.3, 0.7):
            i = int(lbl[int(fy * sh), int(fx * sw)])
            if i == 0 or i in tried:  # on a wall, or a region already scored
                continue
            tried.add(i)
            m = (lbl == i).astype(np.uint8) * 255
            # grow back over the 1-2 px the edge walls ate, and fill small holes (things lying on the tray)
            m = cv2.morphologyEx(cv2.dilate(m, np.ones((5, 5), np.uint8)), cv2.MORPH_CLOSE, np.ones((15, 15), np.uint8))
            cs, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            if not cs:
                continue
            c = max(cs, key=cv2.contourArea)
            area = cv2.contourArea(c) / float(sw * sh)
            if not 0.08 <= area <= 0.9:
                continue
            hull = cv2.convexHull(c)
            quad = None
            peri = cv2.arcLength(hull, True)
            for eps in (0.01, 0.02, 0.03, 0.05, 0.08):
                a = cv2.approxPolyDP(hull, eps * peri, True)
                if len(a) == 4:
                    quad = a.reshape(4, 2).astype(np.float32)
                    break
            if quad is None:
                quad = cv2.boxPoints(cv2.minAreaRect(hull)).astype(np.float32)
            fit = cv2.contourArea(c) / max(1.0, cv2.contourArea(quad))  # 1.0 = region is exactly the quad
            # The tray is the dark pad lying inside the picture: prefer dark regions that do not run off the edges
            # (the floor/wall around it is usually lighter and touches the frame border).
            dark = 1.0 - cv2.mean(lab[:, :, 0], mask=m)[0] / 255.0
            pts = c.reshape(-1, 2)
            edge = np.mean((pts[:, 0] <= 2) | (pts[:, 0] >= sw - 3) | (pts[:, 1] <= 2) | (pts[:, 1] >= sh - 3))
            score = area * min(fit, 1 / fit) ** 4 * dark ** 2 * (1.0 - edge) ** 2
            if best is None or score > best[0]:
                best = (score, quad)
    if best is None:
        return None
    q = order(best[1])
    q[:, 0] /= sw
    q[:, 1] /= sh
    return [[round(float(x), 4), round(float(y), 4)] for x, y in q]


class Tray:
    def __init__(self, data_dir):
        self.path = os.path.join(data_dir, "tray.json")
        self.lock = threading.Lock()
        self.quad, self.frame, self.aspect = None, None, None
        self.enhance = True  # make the black pad evenly black and things on it stand out (views only, not the AI)
        self._cache = {}
        self._bg = {}  # picture size -> (time, background brightness) for enhance
        try:
            with open(self.path, encoding="utf-8") as f:
                self._set(json.load(f))
        except (OSError, ValueError, KeyError, TypeError):
            pass

    @property
    def active(self):
        return bool(cv2 is not None and self.quad)

    def _set(self, d):
        q = d["quad"]
        if len(q) != 4 or not all(len(p) == 2 and all(-0.5 <= float(v) <= 1.5 for v in p) for p in q):
            raise ValueError("quad")
        fw, fh = (int(d.get("frame", [1280, 720])[0]), int(d.get("frame", [1280, 720])[1]))
        qq = order([[float(x), float(y)] for x, y in q]) if np is not None else q
        px = [(x * fw, y * fh) for x, y in qq]
        def dist(a, b):
            return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5
        wd = (dist(px[0], px[1]) + dist(px[3], px[2])) / 2
        ht = (dist(px[0], px[3]) + dist(px[1], px[2])) / 2
        self.quad = [[round(float(x), 4), round(float(y), 4)] for x, y in qq]
        self.frame = [fw, fh]
        self.aspect = float(d.get("aspect") or (wd / max(1.0, ht)))
        self.enhance = bool(d.get("enhance", True))
        self._cache = {}

    def _write(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.info(), f)
        os.replace(tmp, self.path)

    def save(self, quad, frame=None, aspect=None):
        with self.lock:
            self._set({"quad": quad, "frame": frame or self.frame or [1280, 720], "aspect": aspect,
                       "enhance": self.enhance})
            self._write()

    def set_enhance(self, on):
        with self.lock:
            self.enhance = bool(on)
            if self.quad:
                self._write()

    def clear(self):
        with self.lock:
            self.quad = self.frame = self.aspect = None
            self._cache = {}
            try:
                os.remove(self.path)
            except OSError:
                pass

    def info(self):
        return {"quad": self.quad, "frame": self.frame, "aspect": round(self.aspect, 4) if self.aspect else None,
                "enhance": self.enhance}

    def look(self, img, target=24, gain=1.8, sat=1.3):
        """The straightened tray picture made easier to read: light falling unevenly on the pad is evened out
        (its smooth background brightness is subtracted), the pad is pushed down to near black, and whatever lies
        on it (lighter or coloured) gets more contrast and colour. ~5 ms for a 960 px picture; the background is
        re-estimated once a second (light changes slowly)."""
        if not self.enhance:
            return img
        import time
        h, w = img.shape[:2]
        lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB)
        L, a, b = cv2.split(lab)
        key = (w, h)
        t, bg = self._bg.get(key, (0.0, None))
        if bg is None or time.monotonic() - t > 1.0:
            sw = 48
            sh = max(8, int(round(sw * h / w)))
            small = cv2.medianBlur(cv2.resize(L, (sw, sh), interpolation=cv2.INTER_AREA), 7)  # tools don't count
            small = cv2.GaussianBlur(small, (0, 0), 2)
            bg = cv2.resize(small, (w, h), interpolation=cv2.INTER_CUBIC)
            self._bg[key] = (time.monotonic(), bg)
        L = cv2.addWeighted(L, gain, bg, -gain, target)  # target + gain*(L - bg), saturating at 0..255
        a = cv2.addWeighted(a, sat, a, 0, 128 * (1 - sat))
        b = cv2.addWeighted(b, sat, b, 0, 128 * (1 - sat))
        return cv2.cvtColor(cv2.merge((L, a, b)), cv2.COLOR_LAB2BGR)

    # ---- geometry ----
    def _h_norm(self):
        """Homography: frame coords 0..1 -> tray coords 0..1."""
        if "hn" not in self._cache:
            src = np.array(self.quad, dtype=np.float32)
            dst = np.array([[0, 0], [1, 0], [1, 1], [0, 1]], dtype=np.float32)
            self._cache["hn"] = cv2.getPerspectiveTransform(src, dst)
        return self._cache["hn"]

    def warp(self, img, out_w, out_h):
        """BGR frame (any size) -> the tray as a straight out_w x out_h picture."""
        h, w = img.shape[:2]
        key = ("w", w, h, out_w, out_h)
        if key not in self._cache:
            src = np.array([[x * w, y * h] for x, y in self.quad], dtype=np.float32)
            dst = np.array([[0, 0], [out_w, 0], [out_w, out_h], [0, out_h]], dtype=np.float32)
            self._cache[key] = cv2.getPerspectiveTransform(src, dst)
        return cv2.warpPerspective(img, self._cache[key], (out_w, out_h), flags=cv2.INTER_LINEAR,
                                   borderMode=cv2.BORDER_CONSTANT, borderValue=(33, 30, 28))

    def map_box(self, box):
        """[x,y,w,h] in 0..1 of the frame -> [x,y,w,h] in 0..1 of the straightened tray, or None when the box's
        centre is not on the tray."""
        x, y, bw, bh = (float(v) for v in box)
        pts = np.array([[[x, y]], [[x + bw, y]], [[x + bw, y + bh]], [[x, y + bh]], [[x + bw / 2, y + bh / 2]]],
                       dtype=np.float32)
        t = cv2.perspectiveTransform(pts, self._h_norm()).reshape(-1, 2)
        cx, cy = t[4]
        if not (0 <= cx <= 1 and 0 <= cy <= 1):
            return None
        x0, y0 = max(0.0, float(t[:4, 0].min())), max(0.0, float(t[:4, 1].min()))
        x1, y1 = min(1.0, float(t[:4, 0].max())), min(1.0, float(t[:4, 1].max()))
        return [round(x0, 4), round(y0, 4), round(x1 - x0, 4), round(y1 - y0, 4)]
