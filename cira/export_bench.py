"""Export the KitCheck YOLO11n to NCNN at smaller input sizes (no retraining) and compare speed/detections.
   ~/yolo_model/.venv/bin/python export_bench.py ~/yolo_model/cira_tools_yolo11n
Writes <model>/last_ncnn_model_416 and _320 next to the current 640 one."""
import glob
import os
import shutil
import sys
import time

import cv2
import ncnn
import numpy as np

MODEL = os.path.expanduser(sys.argv[1])
SIZES = [640, 416, 320]
KEEP = {"hammer", "screwdriver", "pliers", "wrench", "drill"}
names = [l.strip() for l in open(os.path.join(MODEL, "obj.names"), encoding="utf-8") if l.strip()]


def export(size):
    out = os.path.join(MODEL, "last_ncnn_model_%d" % size)
    if os.path.isdir(out):
        return out
    from ultralytics import YOLO
    tmp = os.path.join(MODEL, "export_tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    shutil.copy(os.path.join(MODEL, "last.pt"), os.path.join(tmp, "last.pt"))
    res = YOLO(os.path.join(tmp, "last.pt")).export(format="ncnn", imgsz=size, half=True)  # fp16 weights: half the file
    shutil.move(str(res), out)
    shutil.rmtree(tmp, ignore_errors=True)
    return out


def load(d):
    n = ncnn.Net()
    n.opt.use_vulkan_compute = False
    n.opt.num_threads = 2
    n.load_param(d + "/model.ncnn.param")
    n.load_model(d + "/model.ncnn.bin")
    return n


def infer(net, im, size, conf=0.45):
    h, w = im.shape[:2]
    r = min(size / h, size / w)
    nh, nw = int(round(h * r)), int(round(w * r))
    top, left = (size - nh) // 2, (size - nw) // 2
    canvas = np.full((size, size, 3), 114, np.uint8)
    canvas[top:top + nh, left:left + nw] = cv2.resize(im, (nw, nh))
    mat = ncnn.Mat.from_pixels(canvas, ncnn.Mat.PixelType.PIXEL_BGR2RGB, size, size)
    mat.substract_mean_normalize([], [1 / 255.0] * 3)
    ex = net.create_extractor()
    ex.input("in0", mat)
    _, out = ex.extract("out0")
    a = np.array(out).reshape(4 + len(names), -1)
    sc = a[4:]
    cls, cf = sc.argmax(0), sc.max(0)
    keep = cf >= conf
    return sorted({names[c] for c in cls[keep]})


imgs = sorted(glob.glob(os.path.join(MODEL, "test_images", "*")) + glob.glob(os.path.expanduser("~/yolo_model/test_images/*")))
imgs = [p for p in imgs if p.lower().endswith((".jpg", ".jpeg", ".png"))][:8]
print("test images:", len(imgs))
for size in SIZES:
    d = os.path.join(MODEL, "last_ncnn_model") if size == 640 else export(size)
    t = time.time()
    net = load(d)
    t_load = time.time() - t
    times, found = [], []
    for p in imgs:
        im = cv2.imread(p)
        t = time.time()
        f = infer(net, im, size)
        times.append(time.time() - t)
        found.append([n for n in f if n in KEEP] or ["-"])
    print("%d: load %.2fs, infer avg %.3fs, model %.1f MB" % (
        size, t_load, sum(times) / max(1, len(times)), os.path.getsize(d + "/model.ncnn.bin") / 1e6))
    print("     tools seen per image:", found)
