"""KitCheck core: SQLite storage and business rules.

Decision layers (from the project report):
  1. AI (CiRA CORE) detects type, box, confidence, QR  -> stored raw in `detections`, never modified
  2. System compares with the Master List               -> evaluate()
  3. Staff confirms; edits need a reason                -> `edits`, audit log
"""
import contextlib
import hashlib
import json
import os
import random
import secrets
import sqlite3
import threading
import time

THRESHOLD = float(os.environ.get("KITCHECK_THRESHOLD", "0.70"))
SCAN_TIMEOUT_S = int(os.environ.get("KITCHECK_SCAN_TIMEOUT", "15"))
SESSION_DAYS = 14

DEFAULT_TYPES = [
    ("plr", "คีม"),
    ("scr", "ไขควง"),
    ("drl", "สว่าน"),
    ("hmr", "ค้อน"),
    ("wrn", "ประแจ"),
]

KIND_TH = {"borrow": "ยืม", "return": "คืน"}


class Err(Exception):
    def __init__(self, status, msg):
        super().__init__(msg)
        self.status = status
        self.msg = msg


def now_ms():
    return int(time.time() * 1000)


def _hash_pw(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 200_000).hex()


def baht(n):
    return f"{int(n or 0):,} บาท"


SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT UNIQUE NOT NULL,
  name TEXT NOT NULL,
  pw_hash TEXT NOT NULL,
  salt TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'staff',
  active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS sessions(
  token TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id),
  created INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS types(
  key TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  sort INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS kits(
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  deposit INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'free'
);
CREATE TABLE IF NOT EXISTS kit_master(
  kit_id TEXT NOT NULL REFERENCES kits(id),
  type TEXT NOT NULL,
  qty INTEGER NOT NULL,
  PRIMARY KEY(kit_id, type)
);
CREATE TABLE IF NOT EXISTS items(
  qr TEXT PRIMARY KEY,
  type TEXT NOT NULL,
  kit_id TEXT NOT NULL REFERENCES kits(id),
  label TEXT NOT NULL DEFAULT 'ปกติ',
  active INTEGER NOT NULL DEFAULT 1,
  replaced_by TEXT,
  created INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS loans(
  id TEXT PRIMARY KEY,
  kit_id TEXT NOT NULL REFERENCES kits(id),
  who TEXT NOT NULL,
  phone TEXT NOT NULL,
  deposit INTEGER NOT NULL,
  out_ts INTEGER NOT NULL,
  due_ts INTEGER NOT NULL,
  borrow_scan TEXT NOT NULL,
  return_scan TEXT,
  status TEXT NOT NULL DEFAULT 'open',
  closed_ts INTEGER,
  refund INTEGER
);
CREATE TABLE IF NOT EXISTS loan_notes(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  loan_id TEXT NOT NULL REFERENCES loans(id),
  ts INTEGER NOT NULL,
  by_user TEXT NOT NULL,
  scan_id TEXT,
  detail TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scans(
  id TEXT PRIMARY KEY,
  kit_id TEXT NOT NULL REFERENCES kits(id),
  kind TEXT NOT NULL,
  loan_id TEXT,
  ts INTEGER NOT NULL,
  dur REAL,
  status TEXT NOT NULL,
  by_user TEXT,
  source TEXT NOT NULL,
  expires_ts INTEGER,
  meta TEXT,
  image TEXT,
  error TEXT
);
CREATE TABLE IF NOT EXISTS detections(
  scan_id TEXT NOT NULL REFERENCES scans(id),
  idx INTEGER NOT NULL,
  type TEXT NOT NULL,
  conf REAL NOT NULL,
  qr TEXT,
  bx REAL, by REAL, bw REAL, bh REAL,
  PRIMARY KEY(scan_id, idx)
);
CREATE TABLE IF NOT EXISTS edits(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  scan_id TEXT NOT NULL REFERENCES scans(id),
  type TEXT NOT NULL,
  from_n INTEGER NOT NULL,
  to_n INTEGER NOT NULL,
  reason TEXT NOT NULL,
  by_user TEXT NOT NULL,
  ts INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS audit(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL,
  who TEXT NOT NULL,
  what TEXT NOT NULL,
  sub TEXT NOT NULL DEFAULT '',
  tag TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS counters(
  name TEXT PRIMARY KEY,
  val INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS station(
  id INTEGER PRIMARY KEY CHECK (id = 1),
  payload TEXT,
  ts INTEGER
);
CREATE INDEX IF NOT EXISTS ix_items_kit ON items(kit_id);
CREATE INDEX IF NOT EXISTS ix_loans_kit ON loans(kit_id, status);
CREATE INDEX IF NOT EXISTS ix_scans_status ON scans(status);
"""


class Store:
    def __init__(self, path, mode="sim"):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.path = path
        self.mode = mode  # 'sim' or 'mqtt'
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA)
        with self.tx() as c:
            if not c.execute("SELECT 1 FROM types LIMIT 1").fetchone():
                for i, (k, n) in enumerate(DEFAULT_TYPES):
                    c.execute("INSERT INTO types(key,name,sort) VALUES(?,?,?)", (k, n, i))
        self.mqtt_connected = False

    # ---------- infrastructure ----------
    @contextlib.contextmanager
    def tx(self):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield self.db
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    def _next(self, c, name, prefix, width):
        row = c.execute("SELECT val FROM counters WHERE name=?", (name,)).fetchone()
        val = (row["val"] if row else 0) + 1
        c.execute("INSERT INTO counters(name,val) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET val=excluded.val", (name, val))
        return f"{prefix}{val:0{width}d}"

    on_event = None  # set by Api: every audit entry also goes out as an MQTT event (CiRA -> LINE etc.)

    def _audit(self, c, who, what, sub="", tag=""):
        c.execute("INSERT INTO audit(ts,who,what,sub,tag) VALUES(?,?,?,?,?)", (now_ms(), who, what, sub, tag))
        if self.on_event:
            try:
                self.on_event({"ts": now_ms(), "who": who, "what": what, "sub": sub, "tag": tag})
            except Exception:
                pass  # notifications must never break the transaction

    def _types(self, c):
        return [dict(r) for r in c.execute("SELECT key,name,sort FROM types ORDER BY sort,key")]

    def _type_name(self, c, key):
        r = c.execute("SELECT name FROM types WHERE key=?", (key,)).fetchone()
        return r["name"] if r else key

    # ---------- users ----------
    def create_user(self, username, name, password, role="staff"):
        username = (username or "").strip()
        if not username or not password or len(password) < 6:
            raise Err(400, "ต้องมีชื่อผู้ใช้ และรหัสผ่านอย่างน้อย 6 ตัวอักษร")
        if role not in ("staff", "admin"):
            raise Err(400, "บทบาทไม่ถูกต้อง")
        salt = secrets.token_hex(16)
        with self.tx() as c:
            if c.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
                raise Err(409, "มีชื่อผู้ใช้นี้แล้ว")
            c.execute("INSERT INTO users(username,name,pw_hash,salt,role) VALUES(?,?,?,?,?)",
                      (username, name or username, _hash_pw(password, salt), salt, role))
        return {"username": username, "name": name or username, "role": role}

    def set_password(self, username, password):
        if not password or len(password) < 6:
            raise Err(400, "รหัสผ่านอย่างน้อย 6 ตัวอักษร")
        salt = secrets.token_hex(16)
        with self.tx() as c:
            cur = c.execute("UPDATE users SET pw_hash=?, salt=? WHERE username=?", (_hash_pw(password, salt), salt, username))
            if cur.rowcount == 0:
                raise Err(404, "ไม่พบผู้ใช้")
            c.execute("DELETE FROM sessions WHERE user_id=(SELECT id FROM users WHERE username=?)", (username,))

    def login(self, username, password):
        with self.tx() as c:
            u = c.execute("SELECT * FROM users WHERE username=? AND active=1", ((username or "").strip(),)).fetchone()
            if not u or not secrets.compare_digest(u["pw_hash"], _hash_pw(password or "", u["salt"])):
                raise Err(401, "ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง")
            token = secrets.token_urlsafe(32)
            c.execute("INSERT INTO sessions(token,user_id,created) VALUES(?,?,?)", (token, u["id"], now_ms()))
            c.execute("DELETE FROM sessions WHERE created < ?", (now_ms() - SESSION_DAYS * 86400000,))
            self._audit(c, u["name"], "เข้าสู่ระบบ", "", "")
        return token, {"username": u["username"], "name": u["name"], "role": u["role"]}

    def logout(self, token):
        with self.tx() as c:
            c.execute("DELETE FROM sessions WHERE token=?", (token,))

    def user_by_token(self, token):
        if not token:
            return None
        with self.lock:
            r = self.db.execute(
                "SELECT u.username,u.name,u.role,s.created FROM sessions s JOIN users u ON u.id=s.user_id "
                "WHERE s.token=? AND u.active=1", (token,)).fetchone()
        if not r or r["created"] < now_ms() - SESSION_DAYS * 86400000:
            return None
        return {"username": r["username"], "name": r["name"], "role": r["role"]}

    def list_users(self):
        with self.lock:
            return [dict(r) for r in self.db.execute("SELECT username,name,role,active FROM users ORDER BY id")]

    # ---------- kits / items ----------
    def create_kit(self, user, name, deposit, master=None):
        if user["role"] != "admin":
            raise Err(403, "เฉพาะผู้ดูแลระบบ")
        with self.tx() as c:
            types = self._types(c)
            master = master or {t["key"]: 1 for t in types}
            kit_id = self._next(c, "kit", "KIT-", 2)
            c.execute("INSERT INTO kits(id,name,deposit,status) VALUES(?,?,?, 'free')",
                      (kit_id, (name or "ชุดเครื่องมือช่างพื้นฐาน").strip(), int(deposit or 0)))
            codes = []
            for t in types:
                qty = int(master.get(t["key"], 0))
                if qty <= 0:
                    continue
                c.execute("INSERT INTO kit_master(kit_id,type,qty) VALUES(?,?,?)", (kit_id, t["key"], qty))
                for _ in range(qty):
                    qr = self._next(c, "item", "ITEM-", 4)
                    c.execute("INSERT INTO items(qr,type,kit_id,created) VALUES(?,?,?,?)", (qr, t["key"], kit_id, now_ms()))
                    codes.append(qr)
            self._audit(c, user["name"], f"เพิ่มชุด {kit_id}", f"{name} · มัดจำ {baht(deposit)} · QR {codes[0]}–{codes[-1]}" if codes else "", "ตั้งค่า")
        return kit_id

    def update_kit(self, user, kit_id, status=None, name=None, deposit=None):
        if user["role"] != "admin":
            raise Err(403, "เฉพาะผู้ดูแลระบบ")
        with self.tx() as c:
            k = c.execute("SELECT * FROM kits WHERE id=?", (kit_id,)).fetchone()
            if not k:
                raise Err(404, "ไม่พบชุด")
            if status is not None:
                if status not in ("free", "fix"):
                    raise Err(400, "สถานะไม่ถูกต้อง")
                if k["status"] == "out":
                    raise Err(409, "ชุดนี้ถูกยืมอยู่")
                c.execute("UPDATE kits SET status=? WHERE id=?", (status, kit_id))
                self._audit(c, user["name"], ("ตั้ง " + kit_id + " เป็นซ่อม") if status == "fix" else ("เปิดใช้งาน " + kit_id), "", "ตั้งค่า")
            if name is not None or deposit is not None:
                c.execute("UPDATE kits SET name=COALESCE(?,name), deposit=COALESCE(?,deposit) WHERE id=?",
                          (name, None if deposit is None else int(deposit), kit_id))
                self._audit(c, user["name"], f"แก้ข้อมูล {kit_id}", f"มัดจำ {baht(deposit)}" if deposit is not None else "", "ตั้งค่า")

    def reissue_item(self, user, qr):
        with self.tx() as c:
            it = c.execute("SELECT * FROM items WHERE qr=? AND active=1", (qr,)).fetchone()
            if not it:
                raise Err(404, "ไม่พบรหัสนี้ หรือถูกยกเลิกแล้ว")
            new = self._next(c, "item", "ITEM-", 4)
            c.execute("INSERT INTO items(qr,type,kit_id,label,created) VALUES(?,?,?,?,?)",
                      (new, it["type"], it["kit_id"], "ออกใหม่", now_ms()))
            c.execute("UPDATE items SET active=0, replaced_by=? WHERE qr=?", (new, qr))
            self._audit(c, user["name"], f"ออกป้าย QR ใหม่ {new}",
                        f"แทน {qr} (ยกเลิกแล้ว) · {self._type_name(c, it['type'])} ใน {it['kit_id']}", "QR")
        return new

    def _resolve_qr(self, c, qr):
        """Follow replaced_by so a reissued label still counts as the same piece."""
        seen = 0
        while qr and seen < 20:
            r = c.execute("SELECT replaced_by FROM items WHERE qr=?", (qr,)).fetchone()
            if not r or not r["replaced_by"]:
                return qr
            qr = r["replaced_by"]
            seen += 1
        return qr

    # ---------- scans ----------
    def _kit_or_404(self, c, kit_id):
        k = c.execute("SELECT * FROM kits WHERE id=?", (kit_id,)).fetchone()
        if not k:
            raise Err(404, "ไม่พบชุด")
        return k

    def start_scan(self, user, kit_id, kind, loan_id=None, meta=None):
        if kind not in ("borrow", "return"):
            raise Err(400, "ชนิดงานไม่ถูกต้อง")
        with self.tx() as c:
            k = self._kit_or_404(c, kit_id)
            if kind == "borrow":
                if k["status"] != "free":
                    raise Err(409, "ชุดนี้ไม่ว่าง")
                loan_id = None
            else:
                l = c.execute("SELECT * FROM loans WHERE id=? AND kit_id=? AND status!='closed'", (loan_id, kit_id)).fetchone()
                if not l:
                    raise Err(404, "ไม่พบรายการยืมที่เปิดอยู่")
            # only one active round per kit: older waiting rounds are cancelled so a late result cannot land on them
            c.execute("UPDATE scans SET status='cancelled' WHERE kit_id=? AND status='waiting'", (kit_id,))
            sid = self._next(c, "scan", "scan_", 4)
            c.execute("INSERT INTO scans(id,kit_id,kind,loan_id,ts,status,by_user,source,expires_ts,meta) "
                      "VALUES(?,?,?,?,?, 'waiting', ?, ?, ?, ?)",
                      (sid, kit_id, kind, loan_id, now_ms(), user["name"], self.mode,
                       now_ms() + SCAN_TIMEOUT_S * 1000, json.dumps(meta or {}, ensure_ascii=False)))
        return sid

    def store_result(self, scan_id, detections, duration=None):
        """Called with CiRA CORE's result (or the simulator). Ignores stale / unknown rounds."""
        with self.tx() as c:
            s = c.execute("SELECT * FROM scans WHERE id=?", (scan_id,)).fetchone()
            if not s or s["status"] != "waiting":
                return False
            if s["expires_ts"] and now_ms() > s["expires_ts"] + 2000:
                return False
            known = {t["key"]: t["key"] for t in self._types(c)}
            known.update({t["name"]: t["key"] for t in self._types(c)})
            for i, d in enumerate(detections or []):
                tp = known.get(str(d.get("type", "")).strip(), str(d.get("type", "")).strip() or "unknown")
                box = d.get("box") or [None, None, None, None]
                qr = d.get("qr") or None
                c.execute("INSERT INTO detections(scan_id,idx,type,conf,qr,bx,by,bw,bh) VALUES(?,?,?,?,?,?,?,?,?)",
                          (scan_id, i, tp, float(d.get("conf", 0)), qr, *[None if v is None else float(v) for v in box[:4]]))
            dur = duration if duration is not None else round((now_ms() - s["ts"]) / 1000, 1)
            c.execute("UPDATE scans SET status='pending', dur=? WHERE id=?", (dur, scan_id))
        return True

    def fail_scan(self, scan_id, msg):
        with self.tx() as c:
            c.execute("UPDATE scans SET status='error', error=? WHERE id=? AND status='waiting'", (msg, scan_id))

    def attach_image(self, scan_id, filename):
        with self.tx() as c:
            c.execute("UPDATE scans SET image=? WHERE id=?", (filename, scan_id))

    def simulate(self, scan_id, scenario):
        """Stand-in for CiRA CORE until the hardware exists."""
        with self.lock:
            s = self.db.execute("SELECT * FROM scans WHERE id=?", (scan_id,)).fetchone()
            base = [r["qr"] for r in self.db.execute("SELECT qr FROM items WHERE kit_id=? AND active=1 ORDER BY qr", (s["kit_id"],))]
            item_type = {r["qr"]: r["type"] for r in self.db.execute("SELECT qr,type FROM items")}
            others = [dict(r) for r in self.db.execute("SELECT qr,type FROM items WHERE kit_id!=? AND active=1", (s["kit_id"],))]
        dets = [{"type": item_type.get(q, "unknown"), "qr": q, "conf": round(0.86 + random.random() * 0.12, 2)} for q in base]
        if dets:
            i = random.randrange(len(dets))
            if scenario == "missing":
                dets.pop(i)
            elif scenario == "low":
                dets[i]["conf"] = 0.55
                dets[i]["qr"] = None
            elif scenario == "swap":
                cand = [o for o in others if o["type"] == dets[i]["type"]]
                if cand:
                    dets[i]["qr"] = random.choice(cand)["qr"]
            elif scenario == "extra":
                dets.append({"type": dets[i]["type"], "qr": None, "conf": 0.82})
        self.store_result(scan_id, dets, round(1.4 + random.random() * 0.8, 1))

    def add_edit(self, user, scan_id, type_key, to_n, reason):
        reason = (reason or "").strip()
        if not reason:
            raise Err(400, "ต้องใส่เหตุผลทุกครั้งที่แก้")
        try:
            to_n = int(to_n)
        except (TypeError, ValueError):
            raise Err(400, "จำนวนไม่ถูกต้อง")
        if to_n < 0 or to_n > 99:
            raise Err(400, "จำนวนไม่ถูกต้อง")
        with self.tx() as c:
            s = c.execute("SELECT * FROM scans WHERE id=?", (scan_id,)).fetchone()
            if not s or s["status"] != "pending":
                raise Err(409, "รอบตรวจนี้ปิดไปแล้ว")
            ev = self._evaluate(c, scan_id)
            row = next((r for r in ev["rows"] if r["key"] == type_key), None)
            if row is None:
                raise Err(400, "ไม่พบชนิดนี้")
            c.execute("INSERT INTO edits(scan_id,type,from_n,to_n,reason,by_user,ts) VALUES(?,?,?,?,?,?,?)",
                      (scan_id, type_key, row["fin"], to_n, reason, user["name"], now_ms()))
            self._audit(c, user["name"], f"แก้จำนวน {row['name']} {row['fin']} → {to_n}",
                        f"{scan_id} · {s['kit_id']} · เหตุผล: {reason}", "แก้โดยคน")

    def cancel_scan(self, user, scan_id):
        with self.tx() as c:
            c.execute("UPDATE scans SET status='cancelled' WHERE id=? AND status IN ('pending','waiting','error')", (scan_id,))

    # ---------- evaluation (layer 2) ----------
    def _expect_qrs(self, c, scan):
        if scan["kind"] != "return" or not scan["loan_id"]:
            return None
        l = c.execute("SELECT borrow_scan, kit_id FROM loans WHERE id=?", (scan["loan_id"],)).fetchone()
        if not l:
            return None
        expect = [self._resolve_qr(c, r["qr"]) for r in
                  c.execute("SELECT qr FROM detections WHERE scan_id=? AND qr IS NOT NULL", (l["borrow_scan"],))]
        # pieces that went out without a readable QR (count was confirmed by staff): accept this kit's own pieces of that type
        types_out = {}
        for r in c.execute("SELECT i.type FROM items i WHERE i.qr IN (%s)" % ",".join("?" * len(expect)), expect) if expect else []:
            types_out[r["type"]] = types_out.get(r["type"], 0) + 1
        for m in c.execute("SELECT type, qty FROM kit_master WHERE kit_id=?", (l["kit_id"],)):
            if types_out.get(m["type"], 0) < m["qty"]:
                expect += [r["qr"] for r in c.execute("SELECT qr FROM items WHERE kit_id=? AND type=? AND active=1", (l["kit_id"], m["type"]))]
        return expect

    def _evaluate(self, c, scan_id):
        s = c.execute("SELECT * FROM scans WHERE id=?", (scan_id,)).fetchone()
        types = self._types(c)
        master = {r["type"]: r["qty"] for r in c.execute("SELECT type,qty FROM kit_master WHERE kit_id=?", (s["kit_id"],))}
        kit_items = {r["qr"] for r in c.execute("SELECT qr FROM items WHERE kit_id=? AND active=1", (s["kit_id"],))}
        expect = self._expect_qrs(c, s)
        dets = [dict(r) for r in c.execute("SELECT * FROM detections WHERE scan_id=? ORDER BY idx", (scan_id,))]
        edits = [dict(r) for r in c.execute("SELECT * FROM edits WHERE scan_id=? ORDER BY id", (scan_id,))]
        keys = [t["key"] for t in types]
        for d in dets:
            if d["type"] not in keys:
                keys.append(d["type"])
        names = {t["key"]: t["name"] for t in types}
        counts = {k: 0 for k in keys}
        for d in dets:
            counts[d["type"]] += 1
        fin = dict(counts)
        for e in edits:
            fin[e["type"]] = e["to_n"]
        rows = []
        for k in keys:
            need = master.get(k, 0)
            if need == 0 and counts[k] == 0 and fin.get(k, 0) == 0:
                continue
            ds = [d for d in dets if d["type"] == k]
            edited = any(e["type"] == k for e in edits)
            st = "ok"
            if fin[k] != need:
                st = "diff"
            elif not edited and any(d["conf"] < THRESHOLD or not d["qr"] for d in ds):
                st = "review"
            elif any(d["qr"] and (d["qr"] not in kit_items or (expect is not None and d["qr"] not in expect)) for d in ds):
                st = "qr"
            rows.append({"key": k, "name": names.get(k, k), "need": need, "ai": counts[k], "fin": fin[k], "edited": edited, "st": st})
        result = "ok"
        for level in ("qr", "diff", "review"):
            if any(r["st"] == level for r in rows):
                result = level
                break
        flags = []
        for d in dets:
            if not d["qr"]:
                flags.append("unread")
            elif d["qr"] not in kit_items:
                owner = c.execute("SELECT kit_id, active FROM items WHERE qr=?", (d["qr"],)).fetchone()
                if owner and not owner["active"]:
                    flags.append("retired")
                else:
                    flags.append("foreign:" + (owner["kit_id"] if owner else "?"))
            elif expect is not None and d["qr"] not in expect:
                flags.append("notsame")
            else:
                flags.append("")
        return {"rows": rows, "result": result, "flags": flags}

    def evaluate(self, scan_id):
        with self.lock:
            return self._evaluate(self.db, scan_id)

    # ---------- transactions (layer 3: staff) ----------
    def confirm_borrow(self, user, scan_id, who, phone, days):
        who, phone = (who or "").strip(), (phone or "").strip()
        if not who or not phone:
            raise Err(400, "ต้องมีชื่อและเบอร์ติดต่อ")
        days = max(1, min(90, int(days or 7)))
        with self.tx() as c:
            s = c.execute("SELECT * FROM scans WHERE id=?", (scan_id,)).fetchone()
            if not s or s["kind"] != "borrow":
                raise Err(404, "ไม่พบรอบตรวจ")
            if s["status"] != "pending":
                raise Err(409, "รอบตรวจนี้ยืนยันหรือยกเลิกไปแล้ว")
            ev = self._evaluate(c, scan_id)
            if ev["result"] != "ok":
                raise Err(409, "ผลสุดท้ายยังไม่ครบ ยืนยันไม่ได้")
            k = self._kit_or_404(c, s["kit_id"])
            if k["status"] != "free":
                raise Err(409, "ชุดนี้ถูกยืมไปแล้ว")
            loan_id = self._next(c, "loan", "LOAN-", 4)
            t = now_ms()
            c.execute("INSERT INTO loans(id,kit_id,who,phone,deposit,out_ts,due_ts,borrow_scan,status) VALUES(?,?,?,?,?,?,?,?, 'open')",
                      (loan_id, k["id"], who, phone, k["deposit"], t, t + days * 86400000, scan_id))
            c.execute("UPDATE scans SET status='confirmed', by_user=? WHERE id=?", (user["name"], scan_id))
            c.execute("UPDATE kits SET status='out' WHERE id=?", (k["id"],))
            n_ed = c.execute("SELECT COUNT(*) n FROM edits WHERE scan_id=?", (scan_id,)).fetchone()["n"]
            self._audit(c, user["name"], f"ยืม {k['id']}",
                        f"{who} · {scan_id} · รับมัดจำ {baht(k['deposit'])}" + (f" · มีการแก้ผล {n_ed} ครั้ง" if n_ed else ""), "ยืม")
        return loan_id

    def confirm_return(self, user, scan_id, loan_id):
        with self.tx() as c:
            s = c.execute("SELECT * FROM scans WHERE id=?", (scan_id,)).fetchone()
            l = c.execute("SELECT * FROM loans WHERE id=?", (loan_id,)).fetchone()
            if not s or not l or s["kind"] != "return" or s["loan_id"] != loan_id:
                raise Err(404, "ไม่พบรายการ")
            if s["status"] != "pending" or l["status"] == "closed":
                raise Err(409, "รายการนี้ถูกดำเนินการไปแล้ว")
            ev = self._evaluate(c, scan_id)
            if ev["result"] != "ok":
                raise Err(409, "ผลสุดท้ายยังไม่ครบ หรือ QR ไม่ตรง ปิดรายการไม่ได้")
            t = now_ms()
            c.execute("UPDATE scans SET status='confirmed', by_user=? WHERE id=?", (user["name"], scan_id))
            c.execute("UPDATE loans SET status='closed', return_scan=?, closed_ts=?, refund=deposit WHERE id=?", (scan_id, t, loan_id))
            c.execute("UPDATE kits SET status=(CASE WHEN status='fix' THEN 'fix' ELSE 'free' END) WHERE id=?", (l["kit_id"],))
            self._audit(c, user["name"], f"คืนครบ {l['kit_id']} · ปิดรายการ", f"{l['who']} · {scan_id} · คืนมัดจำ {baht(l['deposit'])}", "คืนครบ")

    def record_anomaly(self, user, scan_id, loan_id, note=""):
        with self.tx() as c:
            s = c.execute("SELECT * FROM scans WHERE id=?", (scan_id,)).fetchone()
            l = c.execute("SELECT * FROM loans WHERE id=?", (loan_id,)).fetchone()
            if not s or not l or s["loan_id"] != loan_id:
                raise Err(404, "ไม่พบรายการ")
            if s["status"] != "pending":
                raise Err(409, "รอบตรวจนี้ปิดไปแล้ว")
            ev = self._evaluate(c, scan_id)
            parts = []
            for r in ev["rows"]:
                if r["st"] == "diff":
                    parts.append(f"{r['name']} (นับได้ {r['fin']}/{r['need']})")
                elif r["st"] == "qr":
                    parts.append(f"{r['name']} (QR ไม่ตรง)")
                elif r["st"] == "review":
                    parts.append(f"{r['name']} (ต้องตรวจทาน)")
            detail = ", ".join(parts) + (f" · {note.strip()}" if note and note.strip() else "")
            c.execute("UPDATE scans SET status='anomaly', by_user=? WHERE id=?", (user["name"], scan_id))
            c.execute("UPDATE loans SET status='anomaly' WHERE id=?", (loan_id,))
            c.execute("INSERT INTO loan_notes(loan_id,ts,by_user,scan_id,detail) VALUES(?,?,?,?,?)",
                      (loan_id, now_ms(), user["name"], scan_id, detail or "ไม่ระบุ"))
            self._audit(c, user["name"], f"บันทึกความผิดปกติ {l['kit_id']}", f"{detail} · รายการยืมยังเปิด · มัดจำระงับไว้", "ผิดปกติ")

    def close_with_deduction(self, user, loan_id, deduct, reason):
        """Admin closes an anomaly loan after settling (e.g. deducting from the deposit)."""
        if user["role"] != "admin":
            raise Err(403, "เฉพาะผู้ดูแลระบบ")
        reason = (reason or "").strip()
        if not reason:
            raise Err(400, "ต้องใส่เหตุผล")
        with self.tx() as c:
            l = c.execute("SELECT * FROM loans WHERE id=?", (loan_id,)).fetchone()
            if not l or l["status"] != "anomaly":
                raise Err(409, "ปิดได้เฉพาะรายการที่บันทึกความผิดปกติแล้ว")
            deduct = max(0, min(int(deduct or 0), l["deposit"]))
            c.execute("UPDATE loans SET status='closed', closed_ts=?, refund=? WHERE id=?", (now_ms(), l["deposit"] - deduct, loan_id))
            c.execute("UPDATE kits SET status='fix' WHERE id=?", (l["kit_id"],))
            self._audit(c, user["name"], f"ปิดรายการผิดปกติ {l['kit_id']}",
                        f"หักมัดจำ {baht(deduct)} · คืน {baht(l['deposit'] - deduct)} · {reason} · ชุดตั้งเป็นซ่อมจนกว่าจะเติมของ", "ผิดปกติ")

    # ---------- station status (MQTT `status`) ----------
    def set_station(self, payload):
        with self.tx() as c:
            c.execute("INSERT INTO station(id,payload,ts) VALUES(1,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload, ts=excluded.ts",
                      (json.dumps(payload, ensure_ascii=False), now_ms()))

    # ---------- read model for the web page ----------
    def state(self, user):
        with self.lock:
            c = self.db
            types = self._types(c)
            kits = []
            for k in c.execute("SELECT * FROM kits ORDER BY id"):
                master = {r["type"]: r["qty"] for r in c.execute("SELECT type,qty FROM kit_master WHERE kit_id=?", (k["id"],))}
                items = [r["qr"] for r in c.execute("SELECT qr FROM items WHERE kit_id=? AND active=1 ORDER BY qr", (k["id"],))]
                kits.append({"id": k["id"], "name": k["name"], "deposit": k["deposit"], "status": k["status"], "master": master, "items": items})
            items = {r["qr"]: {"qr": r["qr"], "type": r["type"], "kit": r["kit_id"], "label": r["label"], "active": bool(r["active"]),
                               "replaced_by": r["replaced_by"]} for r in c.execute("SELECT * FROM items")}
            loans = []
            for l in c.execute("SELECT * FROM loans ORDER BY out_ts DESC LIMIT 500"):
                notes = [dict(n) for n in c.execute("SELECT ts,by_user,scan_id,detail FROM loan_notes WHERE loan_id=? ORDER BY id", (l["id"],))]
                loans.append({"id": l["id"], "kit": l["kit_id"], "who": l["who"], "phone": l["phone"], "deposit": l["deposit"],
                              "out": l["out_ts"], "due": l["due_ts"], "borrowScan": l["borrow_scan"], "returnScan": l["return_scan"],
                              "status": l["status"], "closed": l["closed_ts"], "refund": l["refund"], "notes": notes})
            needed = {l["borrowScan"] for l in loans if l["status"] != "closed"}
            scan_rows = list(c.execute("SELECT * FROM scans ORDER BY ts DESC LIMIT 300"))
            have = {s["id"] for s in scan_rows}
            for sid in needed - have:
                r = c.execute("SELECT * FROM scans WHERE id=?", (sid,)).fetchone()
                if r:
                    scan_rows.append(r)
            scans = {}
            for s in scan_rows:
                dets = []
                for d in c.execute("SELECT * FROM detections WHERE scan_id=? ORDER BY idx", (s["id"],)):
                    det = {"type": d["type"], "qr": d["qr"], "conf": d["conf"]}
                    if d["bx"] is not None:
                        det["box"] = [d["bx"], d["by"], d["bw"], d["bh"]]
                    dets.append(det)
                edits = [{"type": e["type"], "from": e["from_n"], "to": e["to_n"], "reason": e["reason"], "by": e["by_user"], "time": e["ts"]}
                         for e in c.execute("SELECT * FROM edits WHERE scan_id=? ORDER BY id", (s["id"],))]
                ev = self._evaluate(c, s["id"]) if s["status"] not in ("waiting", "error") else None
                if ev:
                    for d, f in zip(dets, ev["flags"]):
                        d["flag"] = f
                scans[s["id"]] = {"ev": ev and {"rows": ev["rows"], "result": ev["result"]}, "id": s["id"], "kit": s["kit_id"], "kind": KIND_TH.get(s["kind"], s["kind"]), "time": s["ts"],
                                  "dur": s["dur"], "dets": dets, "edits": edits, "status": s["status"], "by": s["by_user"],
                                  "loan": s["loan_id"], "meta": json.loads(s["meta"] or "{}"), "has_image": bool(s["image"]),
                                  "source": s["source"], "error": s["error"]}
            log = [dict(r) for r in c.execute("SELECT ts AS time, who, what, sub, tag FROM audit ORDER BY id DESC LIMIT 500")]
            log.reverse()
            st = c.execute("SELECT payload, ts FROM station WHERE id=1").fetchone()
            station = {"mode": self.mode, "mqtt": self.mqtt_connected, "threshold": THRESHOLD, "timeout": SCAN_TIMEOUT_S,
                       "last": json.loads(st["payload"]) if st and st["payload"] else None, "last_ts": st["ts"] if st else None}
        return {"user": user, "types": types, "kits": kits, "items": items, "loans": loans, "scans": scans, "log": log,
                "station": station, "now": now_ms()}
