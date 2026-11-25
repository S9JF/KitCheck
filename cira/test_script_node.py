"""Run kitcheck_script_node.py's detector outside CiRA on test images.
   ~/yolo_model/.venv/bin/python test_script_node.py kitcheck_script_node.py ~/yolo_model/cira_tools_yolo11n img..."""
import os
import sys
import time

import cv2
import numpy as np

src = open(sys.argv[1], encoding="utf-8").read()
src = src[:src.index("\ntry:\n")]  # functions only, not the per-frame main block
src = src.replace("'/home/cira/Models/cira_tools_yolo11n'", repr(os.path.expanduser(sys.argv[2])))
g = {"cv2": cv2, "np": np}
exec(compile(src, "kitcheck_script_node.py", "exec"), g)
for p in sys.argv[3:]:
    t = time.time()
    r = g["kitcheck_detect"](cv2.imread(p))
    print("%-28s %.2fs %s" % (os.path.basename(p)[:28], time.time() - t,
                              [(d["type"], d["conf"]) for d in r["detections"]]))
print("status:", g["STATUS"])
