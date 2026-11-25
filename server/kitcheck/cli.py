"""Admin commands.

  python3 -m kitcheck.cli create-user <username> --name "ชื่อ" --admin
  python3 -m kitcheck.cli set-password <username>
  python3 -m kitcheck.cli add-kits <count> --deposit 1500 --name "ชุดเครื่องมือช่างพื้นฐาน"
  python3 -m kitcheck.cli list-users
"""
import argparse
import getpass
import os
import sys

from .core import Err, Store


def _store():
    data = os.environ.get("KITCHECK_DATA") or os.path.join(os.getcwd(), "data")
    return Store(os.path.join(os.path.abspath(data), "kitcheck.db"))


def _pw(args):
    if args.password:
        return args.password
    a = getpass.getpass("รหัสผ่าน (อย่างน้อย 6 ตัว): ")
    b = getpass.getpass("พิมพ์อีกครั้ง: ")
    if a != b:
        sys.exit("รหัสผ่านไม่ตรงกัน")
    return a


def main():
    p = argparse.ArgumentParser(prog="kitcheck")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create-user")
    c.add_argument("username")
    c.add_argument("--name", default=None)
    c.add_argument("--admin", action="store_true")
    c.add_argument("--password", default=None, help="ไม่แนะนำ: จะค้างอยู่ใน history ของ shell")
    s = sub.add_parser("set-password")
    s.add_argument("username")
    s.add_argument("--password", default=None)
    k = sub.add_parser("add-kits")
    k.add_argument("count", type=int)
    k.add_argument("--deposit", type=int, default=500)
    k.add_argument("--name", default="ชุดเครื่องมือช่างพื้นฐาน")
    sub.add_parser("list-users")
    a = p.parse_args()
    st = _store()
    try:
        if a.cmd == "create-user":
            u = st.create_user(a.username, a.name or a.username, _pw(a), "admin" if a.admin else "staff")
            print("สร้างผู้ใช้แล้ว:", u)
        elif a.cmd == "set-password":
            st.set_password(a.username, _pw(a))
            print("ตั้งรหัสผ่านใหม่แล้ว")
        elif a.cmd == "add-kits":
            sysuser = {"name": "ระบบ (cli)", "role": "admin"}
            ids = [st.create_kit(sysuser, a.name, a.deposit) for _ in range(a.count)]
            print("เพิ่มชุด:", ", ".join(ids))
        elif a.cmd == "list-users":
            for u in st.list_users():
                print(u)
    except Err as e:
        sys.exit(e.msg)


if __name__ == "__main__":
    main()
