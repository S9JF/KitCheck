"""Inspect best.pt and export it to NCNN the way CiRA CORE's DeepDetect (Yolo11) expects.

CiRA's own trainer saves <dir>/last.pt and exports <dir>/last_ncnn_model (half precision),
so the model folder is laid out the same way.
    python export_model.py ~/yolo_model/best.pt ~/yolo_model/cira_tools_yolo11n
"""
import shutil
import sys
from pathlib import Path

import torch
from ultralytics import YOLO

src, out = Path(sys.argv[1]).expanduser(), Path(sys.argv[2]).expanduser()
ck = torch.load(src, map_location="cpu", weights_only=False)
print("epoch:", ck.get("epoch"), "| date:", ck.get("date"))
for k, v in (ck.get("train_metrics") or {}).items():
    if "mAP" in k or "precision" in k or "recall" in k:
        print(f"  {k}: {float(v):.3f}")

out.mkdir(parents=True, exist_ok=True)
shutil.copy2(src, out / "last.pt")
model = YOLO(str(out / "last.pt"))
names = model.names
print("classes:", len(names), "->", ", ".join(names[i] for i in sorted(names)))
(out / "obj.names").write_text("\n".join(names[i] for i in sorted(names)) + "\n", encoding="utf-8")

model.export(format="ncnn", imgsz=640, half=True, project=str(out), name="last_ncnn_model")
for p in sorted(out.rglob("*")):
    if p.is_file():
        print(f"  {p.relative_to(out)}  {p.stat().st_size:,} B")
