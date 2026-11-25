"""LINE Flex Message cards for KitCheck's CiRA CORE LINE nodes (installed as /home/cira/kitcheck_model/kc_flex.py).
Same colours as the kiosk. Every button sends a plain text command (ถาด / ยืม / สมัคร / เลิก / ช่วยเหลือ), because
CiRA's LineBot node passes text messages to the flow (it does not handle postbacks)."""
import time

INK, MUTED, LINE_C, BG = "#1D1F23", "#5F636A", "#E4E2DC", "#F4F3EF"
PRI, OK, WARN, BAD, INFO = "#C2410C", "#1F7A3F", "#9A5B00", "#B42318", "#3F3D9E"

# model class (English) or KitCheck type key -> (Thai name, colour)
TYPES = {"hammer": ("ค้อน", "#7B5A3C"), "hmr": ("ค้อน", "#7B5A3C"),
         "pliers": ("คีม", "#A3362B"), "plr": ("คีม", "#A3362B"),
         "screwdriver": ("ไขควง", "#C8901A"), "scr": ("ไขควง", "#C8901A"),
         "drill": ("สว่าน", "#2F5F9E"), "drl": ("สว่าน", "#2F5F9E"),
         "wrench": ("ประแจ", "#7D838C"), "wrn": ("ประแจ", "#7D838C")}


def t(text, **kw):
    d = {"type": "text", "text": str(text) if str(text) else " ", "wrap": True}
    d.update(kw)
    return d


def btn(label, text, primary=False):
    b = {"type": "button", "height": "sm", "style": "primary" if primary else "secondary",
         "action": {"type": "message", "label": label[:20], "text": text}}
    if primary:
        b["color"] = PRI
    return b


def header(title, sub, color):
    return {"type": "box", "layout": "vertical", "backgroundColor": color, "paddingAll": "16px", "spacing": "2px",
            "contents": [t(title, color="#FFFFFF", weight="bold", size="lg"),
                         t(sub, color="#FFFFFFCC", size="xs")]}


def bubble(head, body, buttons=None, hero=None):
    b = {"type": "bubble", "size": "mega", "header": head,
         "body": {"type": "box", "layout": "vertical", "spacing": "md", "paddingAll": "16px", "contents": body},
         "styles": {"header": {"backgroundColor": head["backgroundColor"]}}}
    if hero:
        b["hero"] = hero
    if buttons:
        b["footer"] = {"type": "box", "layout": "horizontal", "spacing": "sm", "paddingAll": "12px", "contents": buttons}
    return b


def swatch_row(color, title, sub, sub_color=MUTED, right=None, right_color=MUTED):
    row = [{"type": "box", "layout": "vertical", "width": "6px", "cornerRadius": "3px", "backgroundColor": color,
            "contents": [{"type": "filler"}]},
           {"type": "box", "layout": "vertical", "flex": 1, "spacing": "2px",
            "contents": [t(title, weight="bold", size="sm", color=INK), t(sub, size="xs", color=sub_color)]}]
    if right:
        row.append(t(right, size="xs", color=right_color, flex=0, align="end", gravity="center"))
    return {"type": "box", "layout": "horizontal", "spacing": "md", "contents": row}


def sep():
    return {"type": "separator", "color": LINE_C}


# ---------- cards ----------
def tray(res, img_url=None):
    """latest.json from the vision node -> what is on the tray now, with the AI's picture."""
    dets = (res or {}).get("detections", [])
    ts = (res or {}).get("ts", 0)
    age = time.time() - ts if ts else None
    sub = "%s · %d ชิ้น" % (time.strftime("%H:%M:%S", time.localtime(ts)) if ts else "--:--", len(dets))
    body = []
    if age is None:
        body.append(t("ยังไม่มีผลจาก AI (flow ตรวจภาพยังไม่ทำงาน?)", size="sm", color=BAD))
    elif age > 10:
        body.append(t("ผล AI เก่า %d วินาที: กด START / ButtonRun ใน CiRA" % age, size="xs", color=WARN))
    if not dets and age is not None:
        body.append(t("ไม่มีเครื่องมือบนถาด", size="sm", color=MUTED))
    for d in dets[:12]:
        name, col = TYPES.get(d.get("type"), (d.get("type") or "?", "#9AA0A8"))
        qr = d.get("qr")
        body.append(swatch_row(col, name, qr or "อ่าน QR ไม่ได้", MUTED if qr else WARN,
                               "%d%%" % round((d.get("conf") or 0) * 100)))
    if len(dets) > 12:
        body.append(t("และอีก %d ชิ้น" % (len(dets) - 12), size="xs", color=MUTED))
    hero = {"type": "image", "url": img_url, "size": "full", "aspectRatio": "16:9", "aspectMode": "cover"} if img_url else None
    return bubble(header("ถาดตอนนี้", sub, INK), body or [t(" ")], [btn("รีเฟรช", "ถาด", True), btn("ใครยืมอยู่", "ยืม")], hero)


def loans(s, err=None):
    """/api/station/summary -> who has what."""
    if err:
        return bubble(header("ใครยืมอยู่", "ต่อ KitCheck ไม่ได้", BAD), [t(err, size="sm", color=BAD)], [btn("ลองใหม่", "ยืม", True)])
    ls = s.get("loans", [])
    late = sum(1 for x in ls if x.get("late"))
    if not ls:
        head = header("ใครยืมอยู่", "ตอนนี้ไม่มีใครยืมอุปกรณ์อยู่", OK)
        body = [t("ของอยู่ครบในตู้", size="sm", color=OK, weight="bold")]
    else:
        head = header("กำลังยืมอยู่ %d ชิ้น" % len(ls), ("เกินกำหนด %d ชิ้น" % late) if late else "ยังไม่มีชิ้นไหนเกินกำหนด",
                      BAD if late else PRI)
        body = []
        for i, x in enumerate(ls[:10]):
            if i:
                body.append(sep())
            due = time.strftime("%d/%m %H:%M", time.localtime(x["due"] / 1000))
            name, col = TYPES.get(x.get("type"), (x.get("type_name") or "?", "#9AA0A8"))
            body.append(swatch_row(col, "%s · %s" % (x.get("type_name") or name, x.get("qr", "")), x.get("member", ""),
                                   MUTED, ("เกินกำหนด " if x.get("late") else "คืน ") + due, BAD if x.get("late") else MUTED))
        if len(ls) > 10:
            body.append(t("และอีก %d ชิ้น" % (len(ls) - 10), size="xs", color=MUTED))
    g = (s.get("scale") or {}).get("grams") if s else None
    if g is not None:
        body.append(t("ตาชั่งตอนนี้ %s g" % format(round(g), ","), size="xs", color=MUTED, margin="lg"))
    return bubble(head, body, [btn("รีเฟรช", "ยืม", True), btn("ดูถาด", "ถาด")])


def subscribed(on, count):
    if on:
        return bubble(header("รับแจ้งเตือนแล้ว", "แชตนี้ · ทั้งหมด %d แชต" % count, OK),
                      [t("จะแจ้งทันทีเมื่อมีการยืม คืน หรือรอบที่ถูกระงับ (น้ำหนักไม่ตรง / QR อ่านไม่ได้ / ของแปลกปลอม)",
                         size="sm", color=INK)],
                      [btn("หยุดแจ้งเตือน", "เลิก")])
    return bubble(header("หยุดแจ้งเตือนแล้ว", "แชตนี้จะไม่ได้รับแจ้งเตือน", "#45494F"),
                  [t("กด รับแจ้งเตือน เมื่อต้องการกลับมารับอีกครั้ง", size="sm", color=INK)],
                  [btn("รับแจ้งเตือน", "สมัคร", True)])


def help_card():
    rows = [("ดูถาด", "ภาพถาดตอนนี้ที่ AI เห็น พร้อมรายการของ"),
            ("ใครยืมอยู่", "ของที่ยืมออกไป ใครยืม ต้องคืนเมื่อไร"),
            ("รับแจ้งเตือน", "แจ้งในแชตนี้ทุกครั้งที่ยืม คืน หรือมีปัญหา")]
    body = [swatch_row(PRI, a, b) for a, b in rows]
    body.append(t("กดปุ่มในเมนูด้านล่างแชตได้เลย", size="xs", color=MUTED, margin="lg"))
    return bubble(header("KitCheck บอท", "ตู้ยืม–คืนเครื่องมือ · AI โดย CiRA CORE", INK), body,
                  [btn("ดูถาด", "ถาด", True), btn("ใครยืมอยู่", "ยืม")])


EVENT_STYLE = {"ยืม": (PRI, "ยืมอุปกรณ์"), "คืน": (OK, "คืนอุปกรณ์"), "ผิดปกติ": (BAD, "ผิดปกติ"),
               "รอตรวจ": (INFO, "รอเจ้าหน้าที่ตรวจ")}


def event(ev):
    """A KitCheck event from MQTT kitcheck/event -> alert card."""
    color, label = EVENT_STYLE.get(ev.get("tag"), ("#45494F", ev.get("tag") or "KitCheck"))
    ts = ev.get("ts")
    when = time.strftime("%d/%m %H:%M", time.localtime(ts / 1000)) if isinstance(ts, (int, float)) else ""
    body = [t(ev.get("what") or "-", weight="bold", size="md", color=INK)]
    if ev.get("sub"):
        body.append(t(ev["sub"], size="sm", color=MUTED))
    body.append(sep())
    body.append({"type": "box", "layout": "horizontal", "contents": [
        t("โดย " + (ev.get("who") or "-"), size="xs", color=MUTED, flex=1),
        t(when, size="xs", color=MUTED, flex=0, align="end")]})
    return bubble(header(label, "KitCheck · " + when, color), body, [btn("ดูถาด", "ถาด"), btn("ใครยืมอยู่", "ยืม")])
