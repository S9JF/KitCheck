"""Install the KitCheck rich menu (button panel under the chat) on the LINE bot and check the Flex cards.
Run inside the CiRA container as cira:  python3 ~/kitcheck_model/richmenu_setup.py
Reads LINE_TOKEN from ~/kitcheck_model/line.env (never printed). Each button sends a text command that the
"LINE chat" Python+ node answers with a Flex card."""
import json
import os
import sys
from pathlib import Path

import requests

KC = str(Path.home()) + "/kitcheck_model"
sys.path.insert(0, KC)
import kc_flex as F  # noqa: E402
from kc_ros import settings  # noqa: E402

TOKEN = settings("line.env").get("LINE_TOKEN", "")
if not TOKEN:
    sys.exit("no LINE_TOKEN in line.env")
H = {"Authorization": "Bearer " + TOKEN}
API, DATA = "https://api.line.me/v2/bot", "https://api-data.line.me/v2/bot"
NAME = "KitCheck menu"

# 1. check every card shape with LINE's validator (nothing is sent to anyone)
now = __import__("time").time()
samples = {
    "tray": F.tray({"ts": now, "detections": [{"type": "screwdriver", "conf": 0.8, "qr": "ITEM-0002"},
                                              {"type": "wrench", "conf": 0.88, "qr": None}]},
                   "https://example.com/x.jpg"),
    "tray-empty": F.tray(None),
    "loans": F.loans({"loans": [{"type_name": "ไขควง", "qr": "ITEM-0002", "member": "ทดสอบ", "due": now * 1000, "late": True}],
                      "scale": {"grams": 631}}),
    "loans-none": F.loans({"loans": []}),
    "loans-err": F.loans({}, err="timeout"),
    "sub-on": F.subscribed(True, 2), "sub-off": F.subscribed(False, 1), "help": F.help_card(),
    "event": F.event({"ts": now * 1000, "who": "ตู้หน้าห้อง", "what": "คืนอุปกรณ์ 2 ชิ้น", "sub": "ไขควง ITEM-0002", "tag": "คืน"}),
}
bad = 0
for k, card in samples.items():
    r = requests.post(API + "/message/validate/push", headers=H, timeout=10,
                      json={"messages": [{"type": "flex", "altText": "test", "contents": card}]})
    print("validate %-10s %s %s" % (k, r.status_code, "" if r.ok else r.text[:300]))
    bad += not r.ok
if bad:
    sys.exit("some cards are invalid: rich menu not changed")

# 2. replace our old rich menu (if any)
for m in requests.get(API + "/richmenu/list", headers=H, timeout=10).json().get("richmenus", []):
    if m.get("name") == NAME:
        requests.delete(API + "/richmenu/" + m["richMenuId"], headers=H, timeout=10)
        print("deleted old", m["richMenuId"])

W, Hh = 2500, 843
labels = [("ดูถาด", "ถาด"), ("ใครยืมอยู่", "ยืม"), ("รับแจ้งเตือน", "สมัคร"), ("วิธีใช้", "ช่วยเหลือ")]
cw = W // len(labels)
menu = {"size": {"width": W, "height": Hh}, "selected": True, "name": NAME, "chatBarText": "เมนู KitCheck",
        "areas": [{"bounds": {"x": i * cw, "y": 0, "width": cw, "height": Hh},
                   "action": {"type": "message", "label": lab, "text": cmd}} for i, (lab, cmd) in enumerate(labels)]}
r = requests.post(API + "/richmenu", headers=H, json=menu, timeout=10)
r.raise_for_status()
rid = r.json()["richMenuId"]
with open(os.path.join(KC, "richmenu.png"), "rb") as f:
    r = requests.post(DATA + "/richmenu/%s/content" % rid, headers=dict(H, **{"Content-Type": "image/png"}), data=f.read(), timeout=20)
r.raise_for_status()
r = requests.post(API + "/user/all/richmenu/" + rid, headers=H, timeout=10)
r.raise_for_status()
print("rich menu installed as default:", rid)
