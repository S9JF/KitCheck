// Copy to secrets.h and fill in. secrets.h is in .gitignore: never commit it.
#pragma once

// WiFi for the first boot only: after that, set WiFi on the screen (WiFi chip, top right).
#define WIFI_SSID "CHANGE_ME"
#define WIFI_PASS ""

// KitCheck server. Can also be changed on the screen (WiFi settings -> เซิร์ฟเวอร์).
// On the laptop's Windows hotspot the kiosk uses http://192.168.137.1:8080 automatically.
#define KC_BASE "https://your-kitcheck-server.example"

// The kiosk's KitCheck account (a staff user created in KitCheck)
#define KC_USER "kiosk"
#define KC_PASS "CHANGE_ME"

// Staff PIN for the WiFi / server settings on the screen
#define KIOSK_PIN "1234"
