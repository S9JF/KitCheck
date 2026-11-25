"""Local demo server: simulator mode, throw-away data folder, sample tools and members.

    python -I run_dev.py [port]

Logins (local demo only): admin / demo1234  ·  staff / demo1234
Member cards for the kiosk: type 6501234 (นภา) or 6505678 (ธนพล) and press Enter.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ.setdefault("KITCHECK_DATA", os.path.join(HERE, "dev_data"))
os.environ.setdefault("KITCHECK_MODE", "sim")

from kitcheck.api import get_api  # noqa: E402
from kitcheck import devserver  # noqa: E402

api = get_api()
st, desk = api.store, api.desk
if not st.list_users():
    st.create_user("admin", "ผู้ดูแล (ทดสอบ)", "demo1234", "admin")
    st.create_user("staff", "สมชาย ใจดี", "demo1234", "staff")
    sysu = {"name": "ระบบ (ตัวอย่าง)", "role": "admin"}
    for key, w, n in (("hmr", 620, 3), ("plr", 240, 3), ("scr", 150, 4), ("drl", 1210, 2), ("wrn", 245, 3)):
        desk.create_tools(sysu, key, w, n)
    desk.create_member(sysu, "6501234", "นภา แก้วมณี", "089-555-0123", "6501234")
    desk.create_member(sysu, "6505678", "ธนพล ใจกล้า", "081-234-5678", "6505678")
    desk.create_member(sysu, "6509999", "อารีย์ ทองดี", "085-210-3344", None)
    print("seeded demo data")

port = sys.argv[1] if len(sys.argv) > 1 else "8765"
sys.argv = ["devserver", "--host", "127.0.0.1", "--port", port]
devserver.main()
