"""Self-service, per-item borrowing (KitCheck v2).

People borrow and return single tools at a kiosk instead of fixed kits:
  * borrow: member taps a card, puts any tools on the tray, the station reads
    every tool's QR, the kiosk counts down and the loans are recorded.
  * return: tools are put on the tray without a card; each QR closes the open
    loan of that piece. Pieces the system cannot settle go to a staff queue.

Lives next to the kit flow in core.py and shares its database, users, audit
log and counters (new tools continue the ITEM-xxxx numbering; active items of
the kit model are copied over once so printed labels keep working).
"""
import json
import os
import random

from .core import SCAN_TIMEOUT_S, THRESHOLD, Err, now_ms

LOAN_DAYS = int(os.environ.get("KITCHECK_LOAN_DAYS", "7"))
WEIGHT_TOL_G = float(os.environ.get("KITCHECK_WEIGHT_TOL_G", "30"))
WEIGHT_TOL_PCT = float(os.environ.get("KITCHECK_WEIGHT_TOL_PCT", "5"))
SCALE_FRESH_MS = 5000
EMPTY_G = 30
DAY_MS = 86400000

# Class names produced by the YOLO11n model (tools_yolo/data.yaml) -> KitCheck type keys.
# Classes not listed here are kept as-is and show up as an unknown type.
YOLO_ALIASES = {"hammer": "hmr", "screwdriver": "scr", "pliers": "plr", "wrench": "wrn", "drill": "drl"}

# Used for weight checks until a tool's own weight has been recorded.
DEFAULT_WEIGHT = {"plr": 240, "scr": 150, "drl": 1200, "hmr": 620, "wrn": 245}

SCHEMA = """
CREATE TABLE IF NOT EXISTS tools(
  qr TEXT PRIMARY KEY,
  type TEXT NOT NULL,
  weight_g REAL,
  active INTEGER NOT NULL DEFAULT 1,
  replaced_by TEXT,
  note TEXT NOT NULL DEFAULT '',
  created INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS members(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  code TEXT UNIQUE NOT NULL,
  card TEXT UNIQUE,
  name TEXT NOT NULL,
  phone TEXT NOT NULL DEFAULT '',
  active INTEGER NOT NULL DEFAULT 1,
  created INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS tool_loans(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  qr TEXT NOT NULL REFERENCES tools(qr),
  member_id INTEGER NOT NULL REFERENCES members(id),
  out_ts INTEGER NOT NULL,
  due_ts INTEGER NOT NULL,
  borrow_scan TEXT NOT NULL,
  return_scan TEXT,
  status TEXT NOT NULL DEFAULT 'open',
  closed_ts INTEGER,
  closed_by TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_tool_open_loan ON tool_loans(qr) WHERE status='open';
CREATE INDEX IF NOT EXISTS ix_tool_loans_member ON tool_loans(member_id, status);
CREATE TABLE IF NOT EXISTS desk_scans(
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  member_id INTEGER,
  ts INTEGER NOT NULL,
  dur REAL,
  status TEXT NOT NULL,
  weight_g REAL,
  source TEXT NOT NULL,
  image TEXT,
  error TEXT,
  by_user TEXT,
  resolved_by TEXT,
  resolved_ts INTEGER,
  note TEXT,
  expires_ts INTEGER
);
CREATE INDEX IF NOT EXISTS ix_desk_scans_status ON desk_scans(status);
CREATE TABLE IF NOT EXISTS desk_dets(
  scan_id TEXT NOT NULL REFERENCES desk_scans(id),
  idx INTEGER NOT NULL,
  type TEXT NOT NULL,
  conf REAL NOT NULL,
  qr TEXT,
  bx REAL, by REAL, bw REAL, bh REAL,
  assigned TEXT,
  PRIMARY KEY(scan_id, idx)
);
"""

ISSUE_TH = {
    "unread": "อ่าน QR ไม่ได้",
    "unknown": "QR นี้ไม่ใช่ของในระบบ",
    "retired": "QR นี้ถูกยกเลิกแล้ว",
    "dup": "QR ซ้ำกันบนถาด",
    "out": "ชิ้นนี้ยังถูกยืมอยู่",
    "notout": "ชิ้นนี้ไม่ได้ถูกยืมอยู่",
    "rejected": "เจ้าหน้าที่ระบุว่าไม่ใช่ของเรา",
}
SCAN_ISSUE_TH = {
    "weight": "น้ำหนักบนถาดไม่ตรงกับเครื่องมือที่เห็น",
    "empty": "ไม่พบเครื่องมือบนถาด",
}
# Return-scan problems that need a person; "notout" is only informational.
REVIEW_ISSUES = {"unread", "unknown", "retired", "dup"}
STATUS_TH = {
    "waiting": "กำลังตรวจ", "ready": "รอยืนยันที่ตู้", "blocked": "ไม่ผ่าน", "done": "สำเร็จ",
    "review": "รอเจ้าหน้าที่", "resolved": "เจ้าหน้าที่ปิดแล้ว", "cancelled": "ยกเลิก", "error": "ผิดพลาด",
}


class Desk:
    def __init__(self, store, sim=False):
        self.s = store
        self.sim = sim
        self.virtual = False  # set by Api: the /sim page is the camera and the scale
        self.scale = {"grams": None, "stable": False, "ts": 0, "src": "sim" if sim else "device"}
        self.sim_tray = {"qrs": [], "unread": []}
        with store.lock:
            store.db.executescript(SCHEMA)
        with store.tx() as c:
            if not c.execute("SELECT 1 FROM tools LIMIT 1").fetchone():
                rows = c.execute("SELECT qr, type, created FROM items WHERE active=1").fetchall()
                for r in rows:
                    c.execute("INSERT INTO tools(qr,type,created) VALUES(?,?,?)", (r["qr"], r["type"], r["created"]))

    # ---------- small helpers ----------
    def _types(self, c):
        return {t["key"]: t["name"] for t in self.s._types(c)}

    def _resolve(self, c, qr):
        for _ in range(20):
            r = c.execute("SELECT replaced_by FROM tools WHERE qr=?", (qr,)).fetchone()
            if not r or not r["replaced_by"]:
                return qr
            qr = r["replaced_by"]
        return qr

    def _weight(self, t):
        if t["weight_g"] is not None:
            return float(t["weight_g"])
        return float(DEFAULT_WEIGHT.get(t["type"], 0)) or None

    def _member_row(self, c, member_id):
        return c.execute("SELECT * FROM members WHERE id=?", (member_id,)).fetchone() if member_id else None

    @staticmethod
    def _member_view(m):
        if not m:
            return None
        return {"id": m["id"], "code": m["code"], "name": m["name"], "phone": m["phone"], "card": m["card"], "active": bool(m["active"])}

    # ---------- scale ----------
    def set_scale(self, grams, stable, src="device"):
        try:
            g = float(grams)
        except (TypeError, ValueError):
            raise Err(400, "ค่าน้ำหนักไม่ถูกต้อง")
        if not -5000 < g < 50000:
            raise Err(400, "ค่าน้ำหนักไม่ถูกต้อง")
        self.scale = {"grams": g, "stable": bool(stable), "ts": now_ms(), "src": src}

    def open_loans(self):
        """Tools out right now (for the CiRA LINE bot): type, QR, who, due."""
        with self.s.lock:
            c = self.s.db
            names = self._types(c)
            return [{"qr": r["qr"], "type_name": names.get(r["type"], r["type"]), "member": r["name"],
                     "out": r["out_ts"], "due": r["due_ts"], "late": r["due_ts"] < now_ms()}
                    for r in c.execute("SELECT l.*, t.type, m.name FROM tool_loans l JOIN tools t ON t.qr=l.qr "
                                       "JOIN members m ON m.id=l.member_id WHERE l.status='open' ORDER BY l.due_ts")]

    def catalog(self):
        """Active tools with their (expected) weight and who holds them, for the virtual tray."""
        with self.s.lock:
            c = self.s.db
            names = self._types(c)
            out = []
            for t in c.execute("SELECT t.*, m.name AS holder FROM tools t "
                               "LEFT JOIN tool_loans l ON l.qr=t.qr AND l.status='open' "
                               "LEFT JOIN members m ON m.id=l.member_id WHERE t.active=1 ORDER BY t.type, t.qr"):
                out.append({"qr": t["qr"], "type": t["type"], "type_name": names.get(t["type"], t["type"]),
                            "weight": self._weight(t), "holder": t["holder"]})
        return out

    def scale_view(self):
        sc = self.scale
        # the simulator's tray never goes stale; a real scale must report every few seconds
        fresh = sc.get("src") == "sim" or bool(sc["ts"] and now_ms() - sc["ts"] < SCALE_FRESH_MS)
        return {"grams": None if sc["grams"] is None or not fresh else round(sc["grams"]),
                "stable": bool(fresh and sc["stable"]), "fresh": bool(fresh), "empty_g": EMPTY_G}

    # ---------- members ----------
    def find_member(self, key):
        key = (key or "").strip()
        if not key:
            raise Err(400, "ไม่ได้อ่านบัตร")
        with self.s.lock:
            c = self.s.db
            m = c.execute("SELECT * FROM members WHERE (card=? OR code=?) AND active=1", (key, key)).fetchone()
            if not m:
                raise Err(404, "ไม่พบบัตรนี้ในระบบ ติดต่อเจ้าหน้าที่เพื่อลงทะเบียน")
            out = self._member_loans(c, m["id"])
        v = self._member_view(m)
        v["loans"] = out
        return v

    def _member_loans(self, c, member_id):
        names = self._types(c)
        return [{"id": l["id"], "qr": l["qr"], "type": l["type"], "type_name": names.get(l["type"], l["type"]),
                 "out": l["out_ts"], "due": l["due_ts"]}
                for l in c.execute("SELECT l.*, t.type FROM tool_loans l JOIN tools t ON t.qr=l.qr "
                                   "WHERE l.member_id=? AND l.status='open' ORDER BY l.due_ts", (member_id,))]

    def create_member(self, user, code, name, phone="", card=None):
        if user["role"] not in ("admin", "staff"):
            raise Err(403, "ไม่มีสิทธิ์")
        code, name = (code or "").strip(), (name or "").strip()
        card = (card or "").strip() or None
        if not code or not name:
            raise Err(400, "ต้องมีรหัสสมาชิกและชื่อ")
        with self.s.tx() as c:
            if c.execute("SELECT 1 FROM members WHERE code=? OR (card IS NOT NULL AND card=?)", (code, card)).fetchone():
                raise Err(409, "รหัสหรือบัตรนี้มีในระบบแล้ว")
            c.execute("INSERT INTO members(code,card,name,phone,created) VALUES(?,?,?,?,?)",
                      (code, card, name, (phone or "").strip(), now_ms()))
            self.s._audit(c, user["name"], f"เพิ่มสมาชิก {name}", f"รหัส {code}" + (" · ผูกบัตรแล้ว" if card else ""), "ตั้งค่า")

    def update_member(self, user, member_id, name=None, phone=None, card=None, active=None):
        with self.s.tx() as c:
            m = self._member_row(c, int(member_id))
            if not m:
                raise Err(404, "ไม่พบสมาชิก")
            if card is not None:
                card = card.strip() or None
                if card and c.execute("SELECT 1 FROM members WHERE card=? AND id!=?", (card, m["id"])).fetchone():
                    raise Err(409, "บัตรนี้ผูกกับคนอื่นแล้ว")
                c.execute("UPDATE members SET card=? WHERE id=?", (card, m["id"]))
            if name is not None and name.strip():
                c.execute("UPDATE members SET name=? WHERE id=?", (name.strip(), m["id"]))
            if phone is not None:
                c.execute("UPDATE members SET phone=? WHERE id=?", (phone.strip(), m["id"]))
            if active is not None:
                c.execute("UPDATE members SET active=? WHERE id=?", (1 if active else 0, m["id"]))
            self.s._audit(c, user["name"], f"แก้ข้อมูลสมาชิก {m['name']}", "", "ตั้งค่า")

    # ---------- tools ----------
    def create_tools(self, user, type_key, weight_g=None, count=1, note=""):
        if user["role"] != "admin":
            raise Err(403, "เฉพาะผู้ดูแลระบบ")
        count = max(1, min(50, int(count or 1)))
        w = None if weight_g in (None, "") else float(weight_g)
        if w is not None and not (1 <= w <= 50000):
            raise Err(400, "น้ำหนักไม่ถูกต้อง")
        with self.s.tx() as c:
            names = self._types(c)
            if type_key not in names:
                raise Err(400, "ไม่รู้จักชนิดนี้")
            codes = []
            for _ in range(count):
                qr = self.s._next(c, "item", "ITEM-", 4)
                c.execute("INSERT INTO tools(qr,type,weight_g,note,created) VALUES(?,?,?,?,?)",
                          (qr, type_key, w, (note or "").strip(), now_ms()))
                codes.append(qr)
            self.s._audit(c, user["name"], f"ลงทะเบียน{names[type_key]} {len(codes)} ชิ้น",
                          f"{codes[0]}" + (f"–{codes[-1]}" if len(codes) > 1 else "") + (f" · {w:.0f} g" if w else ""), "QR")
        return codes

    def update_tool(self, user, qr, weight_g=None, active=None, note=None):
        if user["role"] != "admin":
            raise Err(403, "เฉพาะผู้ดูแลระบบ")
        with self.s.tx() as c:
            t = c.execute("SELECT * FROM tools WHERE qr=?", (qr,)).fetchone()
            if not t:
                raise Err(404, "ไม่พบรหัสนี้")
            parts = []
            if weight_g not in (None, ""):
                w = float(weight_g)
                if not (1 <= w <= 50000):
                    raise Err(400, "น้ำหนักไม่ถูกต้อง")
                c.execute("UPDATE tools SET weight_g=? WHERE qr=?", (w, qr))
                parts.append(f"น้ำหนัก {w:.0f} g")
            if active is not None:
                if not active and c.execute("SELECT 1 FROM tool_loans WHERE qr=? AND status='open'", (qr,)).fetchone():
                    raise Err(409, "ชิ้นนี้ถูกยืมอยู่ ปิดใช้งานไม่ได้")
                c.execute("UPDATE tools SET active=? WHERE qr=?", (1 if active else 0, qr))
                parts.append("เปิดใช้งาน" if active else "ปิดใช้งาน")
            if note is not None:
                c.execute("UPDATE tools SET note=? WHERE qr=?", (note.strip(), qr))
            if parts:
                self.s._audit(c, user["name"], f"แก้ข้อมูล {qr}", " · ".join(parts), "ตั้งค่า")

    def reissue_tool(self, user, qr):
        with self.s.tx() as c:
            t = c.execute("SELECT * FROM tools WHERE qr=? AND active=1", (qr,)).fetchone()
            if not t:
                raise Err(404, "ไม่พบรหัสนี้ หรือถูกยกเลิกแล้ว")
            new = self.s._next(c, "item", "ITEM-", 4)
            c.execute("INSERT INTO tools(qr,type,weight_g,note,created) VALUES(?,?,?,?,?)",
                      (new, t["type"], t["weight_g"], t["note"], now_ms()))
            c.execute("UPDATE tools SET active=0, replaced_by=? WHERE qr=?", (new, qr))
            c.execute("UPDATE tool_loans SET qr=? WHERE qr=? AND status='open'", (new, qr))
            self.s._audit(c, user["name"], f"ออกป้าย QR ใหม่ {new}", f"แทน {qr} (ยกเลิกแล้ว)", "QR")
        return new

    # ---------- scans ----------
    def start_scan(self, user, kind, member_id=None):
        if kind not in ("borrow", "return"):
            raise Err(400, "ชนิดงานไม่ถูกต้อง")
        with self.s.tx() as c:
            if kind == "borrow":
                m = self._member_row(c, member_id)
                if not m or not m["active"]:
                    raise Err(404, "ไม่พบสมาชิก แตะบัตรใหม่อีกครั้ง")
            else:
                member_id = None
            # one active round per station: a late result must not land on an older round
            c.execute("UPDATE desk_scans SET status='cancelled' WHERE status IN ('waiting','ready')")
            sid = self.s._next(c, "dscan", "ds_", 5)
            c.execute("INSERT INTO desk_scans(id,kind,member_id,ts,status,source,by_user,expires_ts) "
                      "VALUES(?,?,?,?, 'waiting', ?, ?, ?)",
                      (sid, kind, member_id, now_ms(), "sim" if self.sim else "station", user["name"],
                       now_ms() + SCAN_TIMEOUT_S * 1000))
        return sid

    def fail_scan(self, scan_id, msg):
        with self.s.tx() as c:
            c.execute("UPDATE desk_scans SET status='error', error=? WHERE id=? AND status='waiting'", (msg, scan_id))

    def attach_image(self, scan_id, filename):
        with self.s.tx() as c:
            c.execute("UPDATE desk_scans SET image=? WHERE id=?", (filename, scan_id))

    def store_result(self, scan_id, detections, duration=None):
        """Detections from the station (or the simulator). Ignores stale or unknown rounds."""
        with self.s.tx() as c:
            s = c.execute("SELECT * FROM desk_scans WHERE id=?", (scan_id,)).fetchone()
            if not s or s["status"] != "waiting":
                return False
            if s["expires_ts"] and now_ms() > s["expires_ts"] + 2000:
                return False
            known = dict(YOLO_ALIASES)  # class names of our YOLO11n model -> KitCheck type keys
            for t in self.s._types(c):
                known[t["key"]] = t["key"]
                known[t["name"]] = t["key"]
            for i, d in enumerate(detections or []):
                raw = str(d.get("type", "")).strip()
                tp = known.get(raw, raw or "unknown")
                box = (list(d.get("box") or []) + [None] * 4)[:4]
                c.execute("INSERT INTO desk_dets(scan_id,idx,type,conf,qr,bx,by,bw,bh) VALUES(?,?,?,?,?,?,?,?,?)",
                          (scan_id, i, tp, float(d.get("conf", 0)), (d.get("qr") or None),
                           *[None if v is None else float(v) for v in box]))
            sc = self.scale_view()
            dur = duration if duration is not None else round((now_ms() - s["ts"]) / 1000, 1)
            c.execute("UPDATE desk_scans SET dur=?, weight_g=? WHERE id=?", (dur, sc["grams"], scan_id))
            self._settle(c, scan_id)
        return True

    def _eval(self, c, scan_id):
        s = c.execute("SELECT * FROM desk_scans WHERE id=?", (scan_id,)).fetchone()
        names = self._types(c)
        dets, seen = [], set()
        for d in c.execute("SELECT * FROM desk_dets WHERE scan_id=? ORDER BY idx", (scan_id,)):
            v = {"idx": d["idx"], "type": d["type"], "type_name": names.get(d["type"], d["type"]),
                 "conf": d["conf"], "qr": d["qr"], "issue": None, "weight": None, "loan": None,
                 "assigned": d["assigned"], "low": d["conf"] < THRESHOLD}
            if d["bx"] is not None:
                v["box"] = [d["bx"], d["by"], d["bw"], d["bh"]]
            qr = d["assigned"] if d["assigned"] and d["assigned"] != "reject" else d["qr"]
            if d["assigned"] == "reject":
                v["issue"] = "rejected"
            elif not qr:
                v["issue"] = "unread"
            else:
                qr = self._resolve(c, qr)
                v["qr"] = qr
                t = c.execute("SELECT * FROM tools WHERE qr=?", (qr,)).fetchone()
                if not t:
                    v["issue"] = "unknown"
                elif not t["active"]:
                    v["issue"] = "retired"
                elif qr in seen:
                    v["issue"] = "dup"
                else:
                    seen.add(qr)
                    v["type"], v["type_name"] = t["type"], names.get(t["type"], t["type"])
                    v["weight"] = self._weight(t)
                    loan = c.execute("SELECT l.*, m.name FROM tool_loans l JOIN members m ON m.id=l.member_id "
                                     "WHERE l.qr=? AND (l.status='open' OR l.return_scan=?) ORDER BY l.id DESC LIMIT 1",
                                     (qr, scan_id)).fetchone()
                    if loan:
                        v["loan"] = {"id": loan["id"], "member": loan["name"], "member_id": loan["member_id"],
                                     "due": loan["due_ts"], "status": loan["status"]}
                    open_now = bool(loan and loan["status"] == "open")
                    returned_here = bool(loan and loan["return_scan"] == scan_id)
                    if s["kind"] == "borrow" and open_now and loan["borrow_scan"] != scan_id:
                        v["issue"] = "out"
                    elif s["kind"] == "return" and not open_now and not returned_here:
                        v["issue"] = "notout"
            dets.append(v)
        issues = []
        if not dets:
            issues.append("empty")
        known_w = sum(d["weight"] or 0 for d in dets if d["weight"])
        measured = s["weight_g"]
        expected = None
        if measured is not None and dets and all(d["weight"] for d in dets):
            expected = known_w
            tol = max(WEIGHT_TOL_G, expected * WEIGHT_TOL_PCT / 100)
            if abs(measured - expected) > tol:
                issues.append("weight")
        leftover = None
        if measured is not None and any(d["issue"] == "unread" for d in dets):
            leftover = round(measured - known_w)
        return {"scan": s, "dets": dets, "issues": issues, "expected_g": expected,
                "measured_g": measured, "leftover_g": leftover}

    def _settle(self, c, scan_id):
        ev = self._eval(c, scan_id)
        s, dets = ev["scan"], ev["dets"]
        if s["kind"] == "borrow":
            bad = ev["issues"] or any(d["issue"] for d in dets)
            c.execute("UPDATE desk_scans SET status=? WHERE id=?", ("blocked" if bad else "ready", scan_id))
            if bad:  # tell staff (MQTT event -> CiRA -> LINE): why this borrow round was stopped
                why = [SCAN_ISSUE_TH.get(k, k) for k in ev["issues"]]
                why += ["%s %s: %s" % (d["type_name"], d["qr"] or "", ISSUE_TH.get(d["issue"], d["issue"]))
                        for d in dets if d["issue"]]
                if ev["measured_g"] is not None and ev["expected_g"] is not None:
                    why.append("ชั่งได้ %d g คาดไว้ %d g" % (ev["measured_g"], ev["expected_g"]))
                self.s._audit(c, "ตู้", "รอบยืมถูกระงับ", f"{scan_id} · " + " · ".join(why), "ผิดปกติ")
            return
        # A return round only shows what will be returned; nothing is closed until the person confirms on
        # the kiosk (confirm_return). Returning only part of what one borrowed is allowed.
        ok = [d for d in dets if not d["issue"] and d["loan"] and d["loan"]["status"] == "open"]
        need_person = [d for d in dets if d["issue"] in REVIEW_ISSUES]
        c.execute("UPDATE desk_scans SET status=? WHERE id=?", ("ready" if ok or need_person else "blocked", scan_id))

    def confirm(self, user, scan_id):
        with self.s.lock:
            s = self.s.db.execute("SELECT kind FROM desk_scans WHERE id=?", (scan_id,)).fetchone()
        if not s:
            raise Err(404, "ไม่พบรอบตรวจ")
        return self.confirm_return(user, scan_id) if s["kind"] == "return" else self.confirm_borrow(user, scan_id)

    def confirm_return(self, user, scan_id):
        with self.s.tx() as c:
            s = c.execute("SELECT * FROM desk_scans WHERE id=?", (scan_id,)).fetchone()
            if not s or s["kind"] != "return":
                raise Err(404, "ไม่พบรอบตรวจ")
            if s["status"] != "ready":
                raise Err(409, "รอบนี้ยืนยันหรือยกเลิกไปแล้ว")
            self._close_return(c, scan_id)

    def _close_return(self, c, scan_id):
        ev = self._eval(c, scan_id)
        dets = ev["dets"]
        ok = [d for d in dets if not d["issue"] and d["loan"] and d["loan"]["status"] == "open"]
        need_person = [d for d in dets if d["issue"] in REVIEW_ISSUES]
        if not ok and not need_person:
            c.execute("UPDATE desk_scans SET status='blocked' WHERE id=?", (scan_id,))
            raise Err(409, "ไม่มีชิ้นที่คืนได้แล้ว ลองตรวจใหม่")
        t = now_ms()
        for d in ok:
            c.execute("UPDATE tool_loans SET status='returned', return_scan=?, closed_ts=?, closed_by='ตู้' WHERE id=?",
                      (scan_id, t, d["loan"]["id"]))
        if need_person or "weight" in ev["issues"]:
            status = "review"
        else:
            status = "done"
        c.execute("UPDATE desk_scans SET status=? WHERE id=?", (status, scan_id))
        if ok:
            who = sorted({d["loan"]["member"] for d in ok})
            self.s._audit(c, "ตู้", f"คืน {len(ok)} ชิ้น · {', '.join(who)}",
                          f"{scan_id} · " + " · ".join(f"{d['type_name']} {d['qr']}" for d in ok)
                          + (" · มีชิ้นรอเจ้าหน้าที่" if status == "review" else ""), "คืน")
        if status == "review" and not ok:
            self.s._audit(c, "ตู้", "รอบคืนรอเจ้าหน้าที่ตรวจ", scan_id, "รอตรวจ")

    def confirm_borrow(self, user, scan_id):
        with self.s.tx() as c:
            s = c.execute("SELECT * FROM desk_scans WHERE id=?", (scan_id,)).fetchone()
            if not s or s["kind"] != "borrow":
                raise Err(404, "ไม่พบรอบตรวจ")
            if s["status"] != "ready":
                raise Err(409, "รอบนี้ยืนยันหรือยกเลิกไปแล้ว")
            ev = self._eval(c, scan_id)
            if ev["issues"] or any(d["issue"] for d in ev["dets"]):
                c.execute("UPDATE desk_scans SET status='blocked' WHERE id=?", (scan_id,))
                raise Err(409, "สถานะของบางชิ้นเปลี่ยนไป ลองตรวจใหม่")
            m = self._member_row(c, s["member_id"])
            t = now_ms()
            due = t + LOAN_DAYS * DAY_MS
            for d in ev["dets"]:
                c.execute("INSERT INTO tool_loans(qr,member_id,out_ts,due_ts,borrow_scan) VALUES(?,?,?,?,?)",
                          (d["qr"], m["id"], t, due, scan_id))
            c.execute("UPDATE desk_scans SET status='done' WHERE id=?", (scan_id,))
            self.s._audit(c, "ตู้", f"ยืม {len(ev['dets'])} ชิ้น · {m['name']}",
                          f"{scan_id} · " + " · ".join(f"{d['type_name']} {d['qr']}" for d in ev["dets"]), "ยืม")
        return due

    def cancel_scan(self, user, scan_id):
        with self.s.tx() as c:
            c.execute("UPDATE desk_scans SET status='cancelled' WHERE id=? AND status IN ('waiting','ready','blocked','error')",
                      (scan_id,))

    def resolve(self, user, scan_id, assign, note=""):
        """Staff settles a return round: assign each unreadable/unknown piece to an open loan, or reject it."""
        note = (note or "").strip()
        assign = {int(k): (v or "").strip() for k, v in (assign or {}).items()}
        with self.s.tx() as c:
            ev = self._eval(c, scan_id)
            s = ev["scan"]
            if not s or s["status"] != "review":
                raise Err(409, "รอบนี้ไม่ได้รอเจ้าหน้าที่")
            problems = [d for d in ev["dets"] if d["issue"] in REVIEW_ISSUES]
            parts, t = [], now_ms()
            for d in problems:
                v = assign.get(d["idx"])
                if not v:
                    raise Err(400, "ต้องระบุทุกชิ้นที่มีปัญหา")
                if v == "reject":
                    c.execute("UPDATE desk_dets SET assigned='reject' WHERE scan_id=? AND idx=?", (scan_id, d["idx"]))
                    parts.append(f"ชิ้นที่ {d['idx'] + 1} ไม่ใช่ของเรา")
                    continue
                qr = self._resolve(c, v)
                loan = c.execute("SELECT * FROM tool_loans WHERE qr=? AND status='open'", (qr,)).fetchone()
                if not loan:
                    raise Err(409, f"{qr} ไม่ได้ถูกยืมอยู่")
                c.execute("UPDATE desk_dets SET assigned=? WHERE scan_id=? AND idx=?", (qr, scan_id, d["idx"]))
                c.execute("UPDATE tool_loans SET status='returned', return_scan=?, closed_ts=?, closed_by=? WHERE id=?",
                          (scan_id, t, user["name"], loan["id"]))
                parts.append(f"{d['type_name']} = {qr}")
            if "weight" in ev["issues"] and not problems and not note:
                raise Err(400, "น้ำหนักไม่ตรง ใส่หมายเหตุว่าตรวจแล้วเป็นอย่างไร")
            c.execute("UPDATE desk_scans SET status='resolved', resolved_by=?, resolved_ts=?, note=? WHERE id=?",
                      (user["name"], t, note, scan_id))
            self.s._audit(c, user["name"], f"ปิดรอบคืน {scan_id}", " · ".join(parts + ([note] if note else [])), "แก้โดยคน")

    def mark_lost(self, user, loan_id, note):
        if user["role"] != "admin":
            raise Err(403, "เฉพาะผู้ดูแลระบบ")
        note = (note or "").strip()
        if not note:
            raise Err(400, "ต้องใส่เหตุผล")
        with self.s.tx() as c:
            l = c.execute("SELECT l.*, m.name FROM tool_loans l JOIN members m ON m.id=l.member_id WHERE l.id=?",
                          (int(loan_id),)).fetchone()
            if not l or l["status"] != "open":
                raise Err(409, "รายการนี้ไม่ได้เปิดอยู่")
            c.execute("UPDATE tool_loans SET status='lost', closed_ts=?, closed_by=? WHERE id=?", (now_ms(), user["name"], l["id"]))
            c.execute("UPDATE tools SET active=0, note=? WHERE qr=?", ("หาย: " + note, l["qr"]))
            self.s._audit(c, user["name"], f"บันทึกของหาย {l['qr']}", f"{l['name']} · {note}", "ผิดปกติ")

    # ---------- simulator (until the camera station exists) ----------
    def set_sim_tray(self, qrs, unread=None):
        if not self.sim:
            raise Err(409, "ใช้ได้เฉพาะโหมดจำลอง")
        qrs = [str(q) for q in (qrs or [])][:20]
        unread = [str(q) for q in (unread or []) if q in qrs]
        with self.s.lock:
            c = self.s.db
            total = 0.0
            for q in qrs:
                t = c.execute("SELECT * FROM tools WHERE qr=?", (q,)).fetchone()
                if t:
                    total += self._weight(t) or 0
        self.sim_tray = {"qrs": qrs, "unread": unread}
        grams = total + random.uniform(-3, 3) if qrs else random.uniform(0, 2)
        self.scale = {"grams": grams, "stable": True, "ts": now_ms(), "src": "sim"}

    def simulate(self, scan_id):
        tray = self.sim_tray
        with self.s.lock:
            c = self.s.db
            dets = []
            n = len(tray["qrs"])
            cols = 3 if n > 4 else 2
            for i, q in enumerate(tray["qrs"]):
                t = c.execute("SELECT * FROM tools WHERE qr=?", (q,)).fetchone()
                if not t:
                    continue
                col, row = i % cols, i // cols
                w, h = 0.8 / cols, 0.8 / max(1, (n + cols - 1) // cols)
                dets.append({"type": t["type"], "conf": round(random.uniform(0.86, 0.98), 2),
                             "qr": None if q in tray["unread"] else q,
                             "box": [0.1 + col * w + 0.02, 0.1 + row * h + 0.04, w - 0.04, h - 0.08]})
        self.store_result(scan_id, dets, round(random.uniform(1.2, 2.0), 1))

    # ---------- read models ----------
    def scan_view(self, scan_id):
        with self.s.lock:
            return self._scan_view(self.s.db, scan_id)

    def _scan_view(self, c, scan_id):
        ev = self._eval(c, scan_id)
        s = ev["scan"]
        if not s:
            raise Err(404, "ไม่พบรอบตรวจ")
        m = self._member_row(c, s["member_id"])
        for d in ev["dets"]:
            d["issue_th"] = ISSUE_TH.get(d["issue"]) if d["issue"] else None
        v = {"id": s["id"], "kind": s["kind"], "status": s["status"], "status_th": STATUS_TH.get(s["status"], s["status"]),
             "time": s["ts"], "dur": s["dur"], "member": self._member_view(m), "dets": ev["dets"],
             "issues": [{"key": k, "text": SCAN_ISSUE_TH[k]} for k in ev["issues"]],
             "measured_g": ev["measured_g"], "expected_g": ev["expected_g"], "leftover_g": ev["leftover_g"],
             "has_image": bool(s["image"]), "error": s["error"], "by": s["by_user"], "source": s["source"],
             "resolved_by": s["resolved_by"], "resolved_ts": s["resolved_ts"], "note": s["note"],
             "due": None, "remaining": []}
        if s["kind"] == "borrow" and s["status"] in ("ready", "done"):
            v["due"] = (s["ts"] if s["status"] == "ready" else s["ts"]) + LOAN_DAYS * DAY_MS
            if s["status"] == "done":
                r = c.execute("SELECT due_ts FROM tool_loans WHERE borrow_scan=? LIMIT 1", (scan_id,)).fetchone()
                if r:
                    v["due"] = r["due_ts"]
        if s["kind"] == "return":
            members = {d["loan"]["member_id"]: d["loan"]["member"] for d in ev["dets"] if d["loan"]}
            v["remaining"] = [{"member": name, "loans": self._member_loans(c, mid)} for mid, name in members.items()]
            v["remaining"] = [r for r in v["remaining"] if r["loans"]]
        if s["status"] == "review":
            # candidates for unreadable pieces: open loans of the same type, borrowers in this round first
            mids = {d["loan"]["member_id"] for d in ev["dets"] if d["loan"]}
            for d in ev["dets"]:
                if d["issue"] in REVIEW_ISSUES:
                    cands = []
                    for l in c.execute("SELECT l.*, t.type, t.weight_g, m.name FROM tool_loans l JOIN tools t ON t.qr=l.qr "
                                       "JOIN members m ON m.id=l.member_id WHERE l.status='open' AND t.type=? ORDER BY l.out_ts",
                                       (d["type"],)):
                        w = self._weight(l)
                        cands.append({"qr": l["qr"], "member": l["name"], "weight": w,
                                      "same_member": l["member_id"] in mids,
                                      "weight_match": bool(ev["leftover_g"] is not None and w and
                                                           abs(ev["leftover_g"] - w) <= max(WEIGHT_TOL_G, w * WEIGHT_TOL_PCT / 100))})
                    cands.sort(key=lambda x: (not x["weight_match"], not x["same_member"]))
                    d["candidates"] = cands[:12]
        return v

    def kiosk_state(self):
        out = {"scale": self.scale_view(), "sim": self.sim, "loan_days": LOAN_DAYS}
        if self.sim:
            with self.s.lock:
                c = self.s.db
                names = self._types(c)
                cat = []
                for t in c.execute("SELECT t.*, m.name AS holder FROM tools t "
                                   "LEFT JOIN tool_loans l ON l.qr=t.qr AND l.status='open' "
                                   "LEFT JOIN members m ON m.id=l.member_id WHERE t.active=1 ORDER BY t.type, t.qr"):
                    cat.append({"qr": t["qr"], "type": t["type"], "type_name": names.get(t["type"], t["type"]),
                                "weight": self._weight(t), "holder": t["holder"]})
            out["catalog"] = cat
            out["tray"] = self.sim_tray
        return out

    def state(self, user):
        with self.s.lock:
            c = self.s.db
            names = self._types(c)
            tools = []
            for t in c.execute("SELECT t.*, l.id AS loan_id, l.due_ts, l.out_ts, m.name AS holder, m.id AS holder_id "
                               "FROM tools t LEFT JOIN tool_loans l ON l.qr=t.qr AND l.status='open' "
                               "LEFT JOIN members m ON m.id=l.member_id ORDER BY t.qr"):
                tools.append({"qr": t["qr"], "type": t["type"], "type_name": names.get(t["type"], t["type"]),
                              "weight": t["weight_g"], "weight_est": self._weight(t), "active": bool(t["active"]),
                              "replaced_by": t["replaced_by"], "note": t["note"], "created": t["created"],
                              "loan": t["loan_id"] and {"id": t["loan_id"], "member": t["holder"], "member_id": t["holder_id"],
                                                        "out": t["out_ts"], "due": t["due_ts"]}})
            members = []
            for m in c.execute("SELECT m.*, (SELECT COUNT(*) FROM tool_loans l WHERE l.member_id=m.id AND l.status='open') AS n_open, "
                               "(SELECT COUNT(*) FROM tool_loans l WHERE l.member_id=m.id AND l.status='open' AND l.due_ts<?) AS n_late "
                               "FROM members m ORDER BY m.name", (now_ms(),)):
                v = self._member_view(m)
                v.update({"open": m["n_open"], "late": m["n_late"]})
                members.append(v)
            loans = []
            for l in c.execute("SELECT l.*, t.type, m.name FROM tool_loans l JOIN tools t ON t.qr=l.qr "
                               "JOIN members m ON m.id=l.member_id ORDER BY l.id DESC LIMIT 500"):
                loans.append({"id": l["id"], "qr": l["qr"], "type": l["type"], "type_name": names.get(l["type"], l["type"]),
                              "member": l["name"], "member_id": l["member_id"], "out": l["out_ts"], "due": l["due_ts"],
                              "status": l["status"], "closed": l["closed_ts"], "closed_by": l["closed_by"],
                              "borrow_scan": l["borrow_scan"], "return_scan": l["return_scan"]})
            scans = []
            for s in c.execute("SELECT id FROM desk_scans WHERE status!='waiting' ORDER BY ts DESC LIMIT 200").fetchall():
                scans.append(self._scan_view(c, s["id"]))
            review = [s for s in scans if s["status"] == "review"]
            for s in c.execute("SELECT id FROM desk_scans WHERE status='review' ORDER BY ts").fetchall():
                if not any(r["id"] == s["id"] for r in review):
                    review.append(self._scan_view(c, s["id"]))
            log = [dict(r) for r in c.execute("SELECT ts AS time, who, what, sub, tag FROM audit ORDER BY id DESC LIMIT 300")]
            st = c.execute("SELECT payload, ts FROM station WHERE id=1").fetchone()
        return {"user": user, "types": [{"key": k, "name": v} for k, v in names.items()], "tools": tools, "members": members,
                "loans": loans, "scans": scans, "review": review, "log": log, "scale": self.scale_view(),
                "station": {"mode": "sim" if self.sim else "mqtt", "mqtt": self.s.mqtt_connected, "threshold": THRESHOLD,
                            "timeout": SCAN_TIMEOUT_S,
                            "last": json.loads(st["payload"]) if st and st["payload"] else None,
                            "last_ts": st["ts"] if st else None},
                "loan_days": LOAN_DAYS, "now": now_ms()}
