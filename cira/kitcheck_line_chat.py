#preparation here
# KitCheck LINE chatbot (CiRA CORE Python+ node next to the LineBot node, whose "service name" is /kitcheck_line).
# Someone types (or taps the rich menu) in LINE -> LineBot -> this node's ROS service -> a Flex card back.
#   "ถาด"           : picture of the tray as the AI sees it now (boxes drawn) + what is on it
#   "ยืม" / "สถานะ" : what is out on loan right now, who has it, due date (from KitCheck)
#   "สมัคร"         : get KitCheck alerts in this chat (borrow / return / problems), "เลิก" to stop
#   anything else    : help card
import glob
import json
import os
import sys
import threading
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path.home()) + "/kitcheck_model")
import cv2
import kc_flex as F
from kc_ros import KC, getReqData, makeRespData, inner, settings, subscribers, save_subscribers, find

KITCHECK = settings("station.env").get("KITCHECK_URL", "http://100.101.113.108:8080")
IMAGES = str(Path.home()) + "/linebot/images"  # the LineBot node serves this folder at {PUBLIC_URL}
print("KitCheck LINE chatbot (Flex) ready ->", KITCHECK)


def tray_card():
    try:
        res = json.load(open(KC + "/latest.json", encoding="utf-8"))
    except (OSError, ValueError):
        res = None
    url = None
    img = cv2.imread(KC + "/latest.jpg")
    if img is not None:
        try:
            os.makedirs(IMAGES, exist_ok=True)
            if img.shape[1] > 1024:
                img = cv2.resize(img, (1024, round(img.shape[0] * 1024 / img.shape[1])), interpolation=cv2.INTER_AREA)
            name = "kc_tray_%d.jpg" % int(time.time() * 1000)  # new name each time: LINE caches by URL
            cv2.imwrite(os.path.join(IMAGES, name), img, [cv2.IMWRITE_JPEG_QUALITY, 85])
            url = "{PUBLIC_URL}/" + name
            for old in sorted(glob.glob(os.path.join(IMAGES, "kc_tray_*.jpg")))[:-30]:
                os.remove(old)
        except OSError:
            url = None
    return F.tray(res, url)


def loans_card():
    try:
        with urllib.request.urlopen(KITCHECK + "/api/station/summary", timeout=5) as r:
            return F.loans(json.load(r))
    except Exception as e:
        return F.loans({}, err=repr(e)[:120])


def answer(text, src):
    """LINE text -> Flex bubble (dict)."""
    chat = src.get("group_id") or src.get("user_id")
    text = (text or "").strip()
    if text in ("ถาด", "ดูถาด", "tray"):
        return tray_card()
    if text in ("ยืม", "ใครยืมอยู่", "สถานะ", "status", "loans"):
        return loans_card()
    if text in ("สมัคร", "รับแจ้งเตือน", "subscribe"):
        subs = subscribers()
        if chat:
            subs[chat] = {"type": src.get("type"), "since": time.strftime("%Y-%m-%d %H:%M")}
            save_subscribers(subs)
        return F.subscribed(True, len(subs))
    if text in ("เลิก", "หยุดแจ้งเตือน", "unsubscribe"):
        subs = subscribers()
        subs.pop(chat, None)
        save_subscribers(subs)
        return F.subscribed(False, len(subs))
    return F.help_card()


def _line_service(req):
    from cira_msgs.srv import CiraFlowServiceResponse
    try:
        d = json.loads(req.flow_in.jsonstr or "{}")
    except ValueError:
        d = {}
    try:
        card = answer(str(d.get("msg") or ""), d.get("src") or {})
    except Exception as e:  # never leave the user without an answer
        card = F.bubble(F.header("KitCheck", "เกิดข้อผิดพลาด", F.BAD), [F.t(repr(e)[:200], size="xs")])
    print("LINE:", repr(d.get("msg"))[:40], "->", card["header"]["contents"][0]["text"])
    resp = CiraFlowServiceResponse()
    resp.flow_out.jsonstr = json.dumps({"payload": {"line_flex_msg": [card], "line_send_order": ["flex"]}},
                                       ensure_ascii=False)
    return resp


def _serve():
    import rospy
    from cira_msgs.srv import CiraFlowService
    while not rospy.core.is_initialized():
        time.sleep(0.2)
    rospy.Service("/kitcheck_line", CiraFlowService, _line_service)
    print("ROS service /kitcheck_line ready (LineBot -> service name)")


threading.Thread(target=_serve, daemon=True).start()


def handle_request(req):
    """Also usable as a normal flow node: payload {msg, src} in -> payload.line_flex_msg out."""
    data, img = getReqData(req)
    p = inner(data)
    msg = find(data, lambda d: "msg" in d) or {}
    p["line_flex_msg"] = [answer(str(msg.get("msg") or p.get("msg") or ""), msg.get("src") or p.get("src") or {})]
    p["line_send_order"] = ["flex"]
    return makeRespData(data, None)
