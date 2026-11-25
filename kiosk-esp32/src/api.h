// KitCheck API client. All network work runs on a background task; the UI thread only posts
// requests and picks up finished results, so LVGL never blocks on HTTPS.
#pragma once
#include <Arduino.h>
#include <vector>

struct Loan {
  String type, type_name, qr;
  double due = 0;  // ms since epoch
};
struct Member {
  String id_json;  // the member id exactly as the server sent it (number or string), sent back as-is
  String name, code;
  std::vector<Loan> loans;
};
struct Det {
  String type, type_name, qr, issue, issue_th, owner;  // owner = member holding this tool (if on loan)
  float weight = NAN;
  bool has_loan = false;
};
struct Remaining {
  String member;
  std::vector<Loan> loans;
};
struct Scan {
  String id, kind, status;
  std::vector<Det> dets;
  std::vector<String> issues;
  float measured_g = NAN, expected_g = NAN;
  double due = 0;
  std::vector<Remaining> remaining;
};
struct KioskState {
  bool online = false;  // last /api/kiosk/state succeeded
  float grams = NAN;
  bool stable = false, fresh = false;
  float empty_g = 30;
  bool sim = false, camera_ok = true;
  int loan_days = 7;
};

enum class Op { None, Member, Scan, Confirm };
struct Result {
  Op op = Op::None;
  bool ok = false;
  String error;
  Member member;
  Scan scan;
};

void api_begin();
KioskState api_state();
String api_token();    // current login token ("" until logged in), for the camera task
String api_base();     // KitCheck server address in use (set on the screen; secrets.h KC_BASE by default)
void api_set_base(const String &base);  // save a new address, log in there again
String api_problem();  // "" when connected and logged in, otherwise a short Thai explanation

// Each returns false if another request is still running.
bool api_member(const String &code);
bool api_scan(const char *kind, const String &member_id_json);
bool api_confirm(const String &scan_id);
void api_cancel(const String &scan_id);  // fire-and-forget
bool api_take_result(Result &out);

// ---------- WiFi (set from the screen, kept in NVS; secrets.h WIFI_SSID is only the first-boot default) ----------
struct WifiNet {
  String ssid;
  int rssi = -100;
  bool locked = true;       // needs a password
  bool enterprise = false;  // WPA2/3-Enterprise (username + password): not supported here
};
struct WifiInfo {
  bool ready = false;  // false until the network task has filled it in once
  bool connected = false;
  String ssid, ip;  // ssid = the saved network while not connected
  int rssi = 0;
  bool has_saved = false;   // a network is saved (or comes from secrets.h)
  uint32_t down_ms = 0;     // how long WiFi has been down (0 when connected)
};
WifiInfo wifi_info();
void wifi_scan();                                       // start a scan, results via wifi_take_scan
bool wifi_take_scan(std::vector<WifiNet> &out);         // true once when a scan finished
void wifi_connect(const String &ssid, const String &pass);  // try it; saved only if it works
// 0 = nothing new, 1 = connected (server_ok: KitCheck server answered), -1 = failed (err: Thai reason)
int wifi_take_connect(String &err, bool &server_ok);
void wifi_forget();                                     // drop the saved network and disconnect
int wifi_level(int rssi);                               // 1 (weak) .. 4 (excellent)
