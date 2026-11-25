#preparation here
# KitCheck -> LINE alerts (CiRA CORE Python+ node, after MqttSub on topic kitcheck/event).
# Every KitCheck event (borrow, return, round stopped: weight mismatch / unreadable QR / foreign object, staff
# action) arrives from MqttSub; this node pushes it as a Flex card to everyone who subscribed through the LINE bot
# (tap "รับแจ้งเตือน" in the bot's menu). Needs LINE_TOKEN (Messaging API channel access token) in
# /home/cira/kitcheck_model/line.env - the same channel as the LineBot node.
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path.home()) + "/kitcheck_model")
import requests
import kc_flex as F
from kc_ros import getReqData, makeRespData, inner, settings, subscribers, find

PUSH = "https://api.line.me/v2/bot/message/push"
stat = {"sent": 0}


def push(ev):
    token = settings("line.env").get("LINE_TOKEN", "")
    subs = subscribers()
    if not token:
        return "ยังไม่ได้ใส่ LINE_TOKEN ใน line.env"
    if not subs:
        return "ยังไม่มีคนสมัครรับแจ้งเตือน (กด 'รับแจ้งเตือน' ในเมนูบอท)"
    alt = (ev.get("message") or ev.get("what") or "KitCheck")[:390]  # shown in the chat list / notification
    msg = {"type": "flex", "altText": alt, "contents": F.event(ev)}
    ok = 0
    for to in subs:
        try:
            r = requests.post(PUSH, timeout=8, headers={"Authorization": "Bearer " + token},
                              json={"to": to, "messages": [msg]})
            ok += r.status_code == 200
            if r.status_code != 200:
                print("LINE push", r.status_code, r.text[:200])
        except requests.RequestException as e:
            print("LINE push failed:", repr(e)[:120])
    stat["sent"] += ok
    return "ส่งแล้ว %d/%d" % (ok, len(subs))


print("KitCheck LINE notify (Flex) ready; subscribers:", len(subscribers()))


def handle_request(req):
    data, img = getReqData(req)
    ev = find(data, lambda d: "message" in d and "what" in d)
    p = inner(data)
    if ev:
        # push in the background: the flow never waits on LINE
        threading.Thread(target=lambda: p.update(line_result=push(ev)), daemon=True).start()
        p["line_event"] = {"what": ev.get("what"), "tag": ev.get("tag"), "time": time.strftime("%H:%M:%S")}
    else:
        p["line_event"] = None
    return makeRespData(data, img)
