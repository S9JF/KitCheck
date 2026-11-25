# สคริปต์ติดตั้งเซิร์ฟเวอร์จริง (Linux)

ใช้ตอนตั้ง KitCheck บนเซิร์ฟเวอร์ Ubuntu ที่มีกล้อง USB ตาชั่ง และ MQTT ของจริง
**ไม่ต้องใช้** ถ้ารันเดโมบนเครื่องตัวเอง (`python run_local.py` กับหน้า `/sim` ก็พอ)

สคริปต์เขียนไว้ให้รันจาก `~/kitcheck_v2/` บนเซิร์ฟเวอร์ ด้วย `sudo bash <ชื่อไฟล์>`

| สคริปต์ | ทำอะไร |
|---|---|
| `deploy.sh` | ติดตั้ง/อัปเดต KitCheck ลง `/opt/kitcheck` เป็น systemd service |
| `nginx-setup.sh` | nginx reverse proxy + HTTPS ให้โดเมนของเว็บ |
| `cf-setup.sh` | Cloudflare Tunnel (ทางเลือกแทนการเปิดพอร์ต) |
| `mqtt-setup.sh` | ติดตั้ง Mosquitto (MQTT broker) และผู้ใช้ของ KitCheck / ตาชั่ง |
| `reset-tray.sh` | ตั้งรหัสผ่าน MQTT ของตาชั่งใหม่ |
| `cam-fps.sh` | ตั้งกล้อง USB (ustreamer) ให้ได้ 30 fps ที่ 1280x720 |
| `cira-setup.sh` | เตรียม CiRA CORE ใน Docker: คัดลอกโมเดล YOLO11n (NCNN) เข้า container |
| `cira-sync-model.sh` | คัดลอกโมเดลเข้า container ใหม่หลังเทรนใหม่ |
| `cira-python-deps.sh`, `cira-python-fix.sh` | ติดตั้ง/แก้ไลบรารี Python ใน container ของ CiRA (ultralytics, numpy<2) |
| `cira-projects.sh` | ย้ายไฟล์โปรเจกต์ CiRA (.npj) เข้า–ออก container |
| `cira-export.sh` | ส่งออก CiRA CORE ทั้งชุด (image + volume) ไปเครื่อง GPU บน Google Cloud |
| `no-ai.sh` | ปิด AI ชั่วคราว (กลับไปใช้ผลจำลอง) เพื่อคืน CPU ให้กล้อง |

ลำดับที่ใช้ตอนตั้งเครื่องใหม่: `mqtt-setup.sh` → `deploy.sh` → `nginx-setup.sh` (หรือ `cf-setup.sh`) → `cam-fps.sh` → `cira-setup.sh`

ช่วงแรก CiRA รันบน CPU ของเซิร์ฟเวอร์ ภายหลังย้ายไปเครื่อง GPU (ดู `cira/gpu_station.py` และ `cira-export.sh`)
