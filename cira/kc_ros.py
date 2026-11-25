"""Shared helpers for KitCheck's CiRA CORE Python+ nodes (installed as /home/cira/kitcheck_model/kc_ros.py).
CiRA runs Python+ code on its own, without its template's helpers, and ros_numpy is not installed: this module gives
getReqData / makeRespData, settings files and the LINE subscriber list."""
import json
import os
import sys
from pathlib import Path

import cv2
import numpy as np

HOME = str(Path.home())
KC = HOME + "/kitcheck_model"
for _p in (HOME + "/.cira_core_install/cira_libs_ws/install/lib/python3/dist-packages", "/opt/ros/noetic/lib/python3/dist-packages"):
    if _p not in sys.path:
        sys.path.append(_p)

from cira_msgs.srv import CiraFlowService2Response  # noqa: E402
from sensor_msgs.msg import Image as ROSImage  # noqa: E402

_CH = {"mono8": 1, "8UC1": 1, "bgr8": 3, "rgb8": 3, "8UC3": 3, "bgra8": 4, "rgba8": 4, "8UC4": 4}


def numpify(m):
    ch = _CH.get(m.encoding, 3)
    a = np.frombuffer(m.data, np.uint8).reshape(m.height, m.step)[:, :m.width * ch].reshape(m.height, m.width, ch)
    if m.encoding == "rgb8":
        a = cv2.cvtColor(a, cv2.COLOR_RGB2BGR)
    elif ch == 4:
        a = cv2.cvtColor(a, cv2.COLOR_RGBA2BGR if m.encoding == "rgba8" else cv2.COLOR_BGRA2BGR)
    elif ch == 1:
        a = a[:, :, 0]
    return np.ascontiguousarray(a)


def msgify(img):
    m = ROSImage()
    m.height, m.width = img.shape[:2]
    m.encoding = "mono8" if img.ndim == 2 else "bgr8"
    m.is_bigendian = 0
    m.step = m.width * (1 if img.ndim == 2 else 3)
    m.data = np.ascontiguousarray(img, dtype=np.uint8).tobytes()
    return m


def getReqData(req):
    data = json.loads(req.flow_in.jsonstr or "{}")
    img = numpify(req.flow_in.img) if len(req.flow_in.img.data) > 0 else None
    return data, img


def makeRespData(data, img=None):
    resp = CiraFlowService2Response()
    resp.flow_out.jsonstr = json.dumps(data, ensure_ascii=False)
    if img is not None and isinstance(img, np.ndarray) and img.ndim in (2, 3):
        resp.flow_out.img = msgify(img)
    return resp


def inner(data):
    """CiRA's flow data is {"payload": {...}, ...}; the user's values live in "payload"."""
    if isinstance(data.get("payload"), dict):
        return data["payload"]
    return data


def settings(name):
    """KEY=VALUE file in /home/cira/kitcheck_model (e.g. line.env with LINE_TOKEN=...)."""
    out = {}
    try:
        for line in open(os.path.join(KC, name), encoding="utf-8"):
            k, _, v = line.strip().partition("=")
            if k and not k.startswith("#"):
                out[k.strip()] = v.strip()
    except OSError:
        pass
    return out


SUBS = os.path.join(KC, "line_subscribers.json")


def subscribers():
    try:
        return json.load(open(SUBS, encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_subscribers(d):
    tmp = SUBS + ".tmp"
    json.dump(d, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    os.replace(tmp, SUBS)


def find(obj, pred, depth=0):
    """First dict inside obj (dicts/lists/JSON strings, any depth) for which pred(d) is true."""
    if depth > 6:
        return None
    if isinstance(obj, str) and obj[:1] in "{[":
        try:
            obj = json.loads(obj)
        except ValueError:
            return None
    if isinstance(obj, dict):
        if pred(obj):
            return obj
        obj = list(obj.values())
    if isinstance(obj, list):
        for v in obj:
            r = find(v, pred, depth + 1)
            if r is not None:
                return r
    return None
