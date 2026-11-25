"""Draw the KitCheck LINE rich menu picture (2500x843, 4 buttons) -> richmenu.png.
python make_richmenu_image.py [font_bold.ttf]   (Windows: C:/Windows/Fonts/LeelaUIb.ttf has Thai)"""
import os
import sys

from PIL import Image, ImageDraw, ImageFont

W, H = 2500, 843
BG, TILE, LINE_C, INK = (29, 31, 35), (38, 41, 46), (58, 61, 66), (255, 255, 255)
PRI, OK, MUTED = (194, 65, 12), (31, 122, 63), (163, 166, 173)
FONT = sys.argv[1] if len(sys.argv) > 1 else "C:/Windows/Fonts/LeelaUIb.ttf"
big = ImageFont.truetype(FONT, 92)
small = ImageFont.truetype(FONT.replace("LeelaUIb", "LeelawUI"), 50) if "LeelaUIb" in FONT else ImageFont.truetype(FONT, 50)

BUTTONS = [("ดูถาด", "ภาพถาดที่ AI เห็น", "tray", PRI),
           ("ใครยืมอยู่", "ของที่ยืมออกไป", "list", PRI),
           ("รับแจ้งเตือน", "ยืม · คืน · ผิดปกติ", "bell", OK),
           ("วิธีใช้", "คำสั่งทั้งหมด", "help", (69, 73, 79))]

im = Image.new("RGB", (W, H), BG)
d = ImageDraw.Draw(im)
cw = W // len(BUTTONS)


def icon(kind, cx, cy, col):
    s = 8  # stroke
    if kind == "tray":  # tray with two tools + camera corner marks
        d.rounded_rectangle((cx - 120, cy - 80, cx + 120, cy + 80), 22, outline=INK, width=s)
        d.rounded_rectangle((cx - 80, cy - 48, cx - 40, cy + 48), 10, fill=col)
        d.rounded_rectangle((cx - 15, cy - 30, cx + 85, cy + 10), 10, fill=MUTED)
        for x, y, dx, dy in ((cx - 150, cy - 110, 1, 1), (cx + 150, cy - 110, -1, 1), (cx - 150, cy + 110, 1, -1), (cx + 150, cy + 110, -1, -1)):
            d.line((x, y, x + 40 * dx, y), fill=col, width=s)
            d.line((x, y, x, y + 40 * dy), fill=col, width=s)
    elif kind == "list":
        for i in range(3):
            y = cy - 70 + i * 70
            d.ellipse((cx - 120, y - 18, cx - 84, y + 18), fill=col)
            d.rounded_rectangle((cx - 55, y - 12, cx + 125, y + 12), 12, fill=INK)
    elif kind == "bell":
        d.pieslice((cx - 95, cy - 110, cx + 95, cy + 80), 180, 360, fill=INK)
        d.rectangle((cx - 95, cy - 15, cx + 95, cy + 50), fill=INK)
        d.rounded_rectangle((cx - 125, cy + 45, cx + 125, cy + 75), 14, fill=INK)
        d.ellipse((cx - 30, cy + 80, cx + 30, cy + 130), fill=INK)
        d.ellipse((cx + 55, cy - 125, cx + 125, cy - 55), fill=col)
    else:  # help: circle with i
        d.ellipse((cx - 110, cy - 110, cx + 110, cy + 110), outline=INK, width=s)
        d.ellipse((cx - 16, cy - 72, cx + 16, cy - 40), fill=INK)
        d.rounded_rectangle((cx - 14, cy - 20, cx + 14, cy + 70), 10, fill=INK)


for i, (label, sub, kind, col) in enumerate(BUTTONS):
    x0 = i * cw
    d.rounded_rectangle((x0 + 22, 22, x0 + cw - 22, H - 22), 48, fill=TILE)
    d.rounded_rectangle((x0 + 22, 22, x0 + cw - 22, 40), 9, fill=col)  # accent strip on top
    cx = x0 + cw // 2
    icon(kind, cx, 300, col)
    d.text((cx, 560), label, font=big, fill=INK, anchor="mm")
    d.text((cx, 670), sub, font=small, fill=MUTED, anchor="mm")

out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "richmenu.png")
im.save(out, optimize=True)
print("wrote", out, os.path.getsize(out), "bytes")
