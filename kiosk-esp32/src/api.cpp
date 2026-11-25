#include "api.h"

#include <ArduinoJson.h>
#include <HTTPClient.h>
#include <NetworkClientSecure.h>
#include <Preferences.h>
#include <WiFi.h>
#include <time.h>

#include <algorithm>

#include "ca_roots.h"
#include "cam.h"
#include "secrets.h"

static constexpr uint32_t POLL_MS = 700;

// ---------- shared between the worker and the UI (guarded by mu) ----------
static SemaphoreHandle_t mu;
static TaskHandle_t worker_task;
static KioskState st;
static String problem = "กำลังเชื่อมต่อ Wi-Fi…";

struct Job {
  Op op = Op::None;
  String a, b;  // member: code | scan: kind, member id | confirm: scan id
};
static Job job;           // the one request in flight (the UI never has two at once)
static String cancel_id;  // a cancel waiting to be sent
static Result result;
static bool result_ready = false;

struct Lock {
  Lock() { xSemaphoreTake(mu, portMAX_DELAY); }
  ~Lock() { xSemaphoreGive(mu); }
};

KioskState api_state() { Lock l; return st; }
String api_problem() { Lock l; return problem; }
static void set_problem(const String &p) { Lock l; problem = p; }

static bool post_job(Op op, const String &a, const String &b = "") {
  {
    Lock l;
    if (job.op != Op::None || result_ready) return false;
    job.op = op;
    job.a = a;
    job.b = b;
  }
  xTaskNotifyGive(worker_task);
  return true;
}
bool api_member(const String &code) { return post_job(Op::Member, code); }
bool api_scan(const char *kind, const String &member_id_json) { return post_job(Op::Scan, kind, member_id_json); }
bool api_confirm(const String &scan_id) { return post_job(Op::Confirm, scan_id); }
void api_cancel(const String &scan_id) {
  { Lock l; cancel_id = scan_id; }
  xTaskNotifyGive(worker_task);
}
bool api_take_result(Result &out) {
  Lock l;
  if (!result_ready) return false;
  out = result;
  result = Result();
  result_ready = false;
  return true;
}

// ---------- HTTP ----------
static NetworkClientSecure tls;
static NetworkClient plain;
static HTTPClient http;
static String token;
static String token_shared;  // copy of token for other tasks (the camera), guarded by mu
static Preferences prefs;

String api_token() { Lock l; return token_shared; }
static String base;         // worker's copy of the server address
static String base_shared;  // the same for other tasks, guarded by mu
String api_base() { Lock l; return base_shared; }

// Sends one request and parses the JSON reply. Returns the HTTP status, or a negative
// HTTPClient error when the server could not be reached.
static int request(const char *method, const char *path, const String &body, JsonDocument &doc,
                   const JsonDocument *filter, uint32_t timeout_ms) {
  NetworkClient &c = base.startsWith("https://") ? (NetworkClient &)tls : plain;
  if (!http.begin(c, base + path)) return HTTPC_ERROR_CONNECTION_REFUSED;
  http.setReuse(true);  // keep the TLS session open between polls
  http.setTimeout(timeout_ms);
  http.setConnectTimeout(8000);
  http.addHeader("Content-Type", "application/json");
  if (token.length()) http.addHeader("Authorization", "Bearer " + token);
  int code = http.sendRequest(method, body);
  doc.clear();
  if (code > 0) {
    String s = http.getString();
    if (filter) deserializeJson(doc, s, DeserializationOption::Filter(*filter));
    else deserializeJson(doc, s);
  }
  http.end();
  if (code < 0) {
    http.setReuse(false);  // drop the broken connection so the next call reconnects
    http.end();
    c.stop();
  }
  return code;
}

static bool login() {
  if (strcmp(KC_PASS, "CHANGE_ME") == 0) {
    set_problem("ยังไม่ได้ตั้งรหัสผ่านตู้ (KC_PASS ใน secrets.h)");
    return false;
  }
  JsonDocument body, doc;
  body["username"] = KC_USER;
  body["password"] = KC_PASS;
  String b;
  serializeJson(body, b);
  token = "";
  int code = request("POST", "/api/login", b, doc, nullptr, 15000);
  if (code == 200 && doc["token"].is<const char *>()) {
    token = doc["token"].as<String>();
    prefs.putString("token", token);
    { Lock l; token_shared = token; }
    Serial.println("[api] logged in");
    return true;
  }
  if (code < 0) set_problem("เชื่อมต่อเซิร์ฟเวอร์ไม่ได้");
  else set_problem(String("เข้าสู่ระบบไม่ได้: ") + (doc["error"] | "รหัสผ่านตู้ไม่ถูกต้อง"));
  Serial.printf("[api] login failed: %d\n", code);
  return false;
}

// request() plus one automatic re-login when the token has expired.
static int call(const char *method, const char *path, const String &body, JsonDocument &doc,
                const JsonDocument *filter, uint32_t timeout_ms) {
  if (!token.length() && !login()) return 401;
  int code = request(method, path, body, doc, filter, timeout_ms);
  if (code == 401) {
    prefs.remove("token");
    if (!login()) return 401;
    code = request(method, path, body, doc, filter, timeout_ms);
  }
  return code;
}

// ---------- JSON -> structs ----------
static float num_or_nan(JsonVariantConst v) { return v.isNull() ? NAN : v.as<float>(); }

static void parse_loans(JsonArrayConst a, std::vector<Loan> &out) {
  out.clear();
  for (JsonObjectConst l : a) {
    Loan x;
    x.type = l["type"] | "";
    x.type_name = l["type_name"] | "";
    x.qr = l["qr"] | "";
    x.due = l["due"] | 0.0;
    out.push_back(x);
  }
}

static void parse_scan(JsonObjectConst s, Scan &sc) {
  sc = Scan();
  sc.id = s["id"] | "";
  sc.kind = s["kind"] | "";
  sc.status = s["status"] | "";
  sc.measured_g = num_or_nan(s["measured_g"]);
  sc.expected_g = num_or_nan(s["expected_g"]);
  sc.due = s["due"] | 0.0;
  for (JsonObjectConst d : s["dets"].as<JsonArrayConst>()) {
    Det x;
    x.type = d["type"] | "";
    x.type_name = d["type_name"] | "";
    x.qr = d["qr"] | "";
    x.issue = d["issue"] | "";
    x.issue_th = d["issue_th"] | "";
    x.weight = num_or_nan(d["weight"]);
    x.has_loan = d["loan"].is<JsonObjectConst>();
    if (x.has_loan) x.owner = d["loan"]["member"] | "";
    sc.dets.push_back(x);
  }
  for (JsonObjectConst i : s["issues"].as<JsonArrayConst>()) sc.issues.push_back(i["text"] | "");
  for (JsonObjectConst r : s["remaining"].as<JsonArrayConst>()) {
    Remaining x;
    x.member = r["member"] | "";
    parse_loans(r["loans"], x.loans);
    sc.remaining.push_back(x);
  }
}

static JsonDocument state_filter, scan_filter;

static void build_filters() {
  state_filter["scale"] = true;
  state_filter["sim"] = true;
  state_filter["loan_days"] = true;
  state_filter["mode"] = true;
  state_filter["mqtt"] = true;
  state_filter["error"] = true;

  JsonObject s = scan_filter["scan"].to<JsonObject>();
  for (const char *k : {"id", "kind", "status", "measured_g", "expected_g", "due"}) s[k] = true;
  JsonObject d = s["dets"].add<JsonObject>();
  for (const char *k : {"type", "type_name", "qr", "issue", "issue_th", "weight"}) d[k] = true;
  d["loan"]["member"] = true;
  s["issues"].add<JsonObject>()["text"] = true;
  JsonObject r = s["remaining"].add<JsonObject>();
  r["member"] = true;
  r["loans"] = true;
  scan_filter["error"] = true;
}

static String err_text(int code, JsonDocument &doc) {
  if (code < 0) return "เชื่อมต่อเซิร์ฟเวอร์ไม่ได้ ลองใหม่อีกครั้ง";
  if (doc["error"].is<const char *>()) return doc["error"].as<String>();
  return "ผิดพลาด " + String(code);
}

// ---------- WiFi: the screen asks, the worker does it (all WiFi calls stay on this task) ----------
enum class WCmd { None, Scan, Connect, Forget, Base };
static WCmd wcmd = WCmd::None;  // guarded by mu
static String wcmd_ssid, wcmd_pass;
static std::vector<WifiNet> scan_out;
static bool scan_ready = false;
static int conn_result = 0;
static String conn_err;
static bool conn_server_ok = false;
static WifiInfo winfo;
static String saved_ssid, saved_pass;  // worker only
static volatile uint8_t last_reason = 0;

int wifi_level(int r) { return r >= -55 ? 4 : r >= -65 ? 3 : r >= -75 ? 2 : 1; }
WifiInfo wifi_info() { Lock l; return winfo; }
static void wifi_post(WCmd c, const String &ssid = "", const String &pass = "") {
  {
    Lock l;
    wcmd = c;
    wcmd_ssid = ssid;
    wcmd_pass = pass;
  }
  xTaskNotifyGive(worker_task);
}
void wifi_scan() { wifi_post(WCmd::Scan); }
void wifi_connect(const String &ssid, const String &pass) { wifi_post(WCmd::Connect, ssid, pass); }
void wifi_forget() { wifi_post(WCmd::Forget); }
void api_set_base(const String &b) { wifi_post(WCmd::Base, b); }
bool wifi_take_scan(std::vector<WifiNet> &out) {
  Lock l;
  if (!scan_ready) return false;
  out = scan_out;
  scan_ready = false;
  return true;
}
int wifi_take_connect(String &err, bool &server_ok) {
  Lock l;
  int r = conn_result;
  conn_result = 0;
  err = conn_err;
  server_ok = conn_server_ok;
  return r;
}

static void load_wifi() {
  if (prefs.isKey("wifi_ssid")) {  // set from the screen (may be "" after "forget")
    saved_ssid = prefs.getString("wifi_ssid", "");
    saved_pass = prefs.getString("wifi_pass", "");
  } else {  // first boot: the network from secrets.h
    saved_ssid = WIFI_SSID;
    saved_pass = WIFI_PASS;
    if (saved_ssid == "CHANGE_ME") saved_ssid = "";
  }
}
static void begin_saved() {
  if (saved_ssid.length()) WiFi.begin(saved_ssid.c_str(), saved_pass.length() ? saved_pass.c_str() : nullptr);
}
static void drop_connections() {  // the old TCP/TLS connection is dead after a network change
  http.setReuse(false);
  http.end();
  tls.stop();
  plain.stop();
}

static void do_scan() {
  bool up = WiFi.status() == WL_CONNECTED;
  if (!up) WiFi.disconnect(false, false);  // stop reconnect attempts so the radio is free to scan
  int n = WiFi.scanNetworks(false, false);
  std::vector<WifiNet> out;
  for (int i = 0; i < n; i++) {
    String s = WiFi.SSID(i);
    if (!s.length()) continue;
    int r = WiFi.RSSI(i);
    bool dup = false;
    for (auto &x : out)
      if (x.ssid == s) {  // several access points with one name: keep the strongest
        if (r > x.rssi) x.rssi = r;
        dup = true;
        break;
      }
    if (dup) continue;
    wifi_auth_mode_t e = WiFi.encryptionType(i);
    WifiNet w;
    w.ssid = s;
    w.rssi = r;
    w.locked = e != WIFI_AUTH_OPEN;
    w.enterprise = e == WIFI_AUTH_WPA2_ENTERPRISE || e == WIFI_AUTH_WPA3_ENTERPRISE ||
                   e == WIFI_AUTH_WPA2_WPA3_ENTERPRISE || e == WIFI_AUTH_WPA_ENTERPRISE;
    out.push_back(w);
  }
  WiFi.scanDelete();
  std::sort(out.begin(), out.end(), [](const WifiNet &a, const WifiNet &b) { return a.rssi > b.rssi; });
  if (out.size() > 20) out.resize(20);
  if (!up) begin_saved();
  Serial.printf("[wifi] scan: %d found, %u listed\n", n, (unsigned)out.size());
  Lock l;
  scan_out = out;
  scan_ready = true;
}

static String reason_text(uint8_t r, wl_status_t s) {
  if (r == WIFI_REASON_AUTH_FAIL || r == WIFI_REASON_4WAY_HANDSHAKE_TIMEOUT || r == WIFI_REASON_HANDSHAKE_TIMEOUT ||
      r == WIFI_REASON_AUTH_EXPIRE || r == WIFI_REASON_MIC_FAILURE)
    return "รหัสผ่านไม่ถูกต้อง";
  if (r == WIFI_REASON_NO_AP_FOUND || s == WL_NO_SSID_AVAIL) return "หาเครือข่ายนี้ไม่เจอ (เช็กชื่อ หรือเป็น 5 GHz?)";
  return "หมดเวลา: สัญญาณอ่อนหรือรหัสผ่านผิด ลองย้ายตู้ให้ใกล้เราเตอร์";
}

static void do_connect(const String &ssid, const String &pass) {
  Serial.printf("[wifi] trying '%s'\n", ssid.c_str());
  drop_connections();
  WiFi.disconnect(false, false);
  vTaskDelay(pdMS_TO_TICKS(200));
  last_reason = 0;
  WiFi.begin(ssid.c_str(), pass.length() ? pass.c_str() : nullptr);
  uint32_t t0 = millis();
  wl_status_t s;
  while ((s = WiFi.status()) != WL_CONNECTED && millis() - t0 < 15000) {
    uint8_t r = last_reason;
    if (millis() - t0 > 2500 && (r == WIFI_REASON_AUTH_FAIL || r == WIFI_REASON_4WAY_HANDSHAKE_TIMEOUT ||
                                 r == WIFI_REASON_HANDSHAKE_TIMEOUT || r == WIFI_REASON_NO_AP_FOUND))
      break;
    vTaskDelay(pdMS_TO_TICKS(100));
  }
  if (s == WL_CONNECTED) {
    saved_ssid = ssid;
    saved_pass = pass;
    prefs.putString("wifi_ssid", ssid);
    prefs.putString("wifi_pass", pass);
    JsonDocument d;
    int code = request("GET", "/api/kiosk/state", "", d, &state_filter, 8000);  // any HTTP answer = reachable
    Serial.printf("[wifi] connected to '%s', IP %s, server %d\n", ssid.c_str(), WiFi.localIP().toString().c_str(), code);
    Lock l;
    conn_result = 1;
    conn_err = "";
    conn_server_ok = code > 0;
    return;
  }
  String err = reason_text(last_reason, s);
  Serial.printf("[wifi] '%s' failed: reason %u status %d\n", ssid.c_str(), (unsigned)last_reason, (int)s);
  WiFi.disconnect(false, false);
  begin_saved();  // back to the network that was there before
  Lock l;
  conn_result = -1;
  conn_err = err;
  conn_server_ok = false;
}

static void wifi_service() {
  WCmd c;
  String ssid, pass;
  {
    Lock l;
    c = wcmd;
    ssid = wcmd_ssid;
    pass = wcmd_pass;
    wcmd = WCmd::None;
    wcmd_pass = "";
  }
  if (c == WCmd::Scan) do_scan();
  else if (c == WCmd::Connect) do_connect(ssid, pass);
  else if (c == WCmd::Forget) {
    prefs.putString("wifi_ssid", "");
    prefs.putString("wifi_pass", "");
    saved_ssid = saved_pass = "";
    drop_connections();
    WiFi.disconnect(false, false);
    Serial.println("[wifi] forgot the saved network");
  } else if (c == WCmd::Base && ssid.length()) {  // new server: its own login
    base = ssid;
    prefs.putString("base", base);
    prefs.remove("token");
    token = "";
    drop_connections();
    {
      Lock l;
      base_shared = base;
      token_shared = "";
      st.online = false;
    }
    Serial.printf("[api] server -> %s\n", base.c_str());
  }

  static uint32_t down_since = millis();
  bool up = WiFi.status() == WL_CONNECTED;
  if (up) down_since = 0;
  else if (!down_since) down_since = millis();
  WifiInfo w;
  w.ready = true;
  w.connected = up;
  w.ssid = up ? WiFi.SSID() : saved_ssid;
  w.ip = up ? WiFi.localIP().toString() : String();
  w.rssi = up ? WiFi.RSSI() : 0;
  w.has_saved = saved_ssid.length() > 0;
  w.down_ms = up ? 0 : millis() - down_since;
  Lock l;
  winfo = w;
}

// ---------- worker ----------
static void poll_state() {
  JsonDocument doc;
  int code = call("GET", "/api/kiosk/state", "", doc, &state_filter, 8000);
  Lock l;
  if (code != 200) {
    st.online = false;
    if (code != 401) problem = "เชื่อมต่อเซิร์ฟเวอร์ไม่ได้";
    return;
  }
  JsonObjectConst sc = doc["scale"];
  st.online = true;
  st.grams = num_or_nan(sc["grams"]);
  st.stable = sc["stable"] | false;
  st.fresh = sc["fresh"] | false;
  st.empty_g = sc["empty_g"] | 30.0f;
  st.sim = doc["sim"] | false;
  st.loan_days = doc["loan_days"] | 7;
  st.camera_ok = strcmp(doc["mode"] | "", "mqtt") != 0 || (doc["mqtt"] | false);
  problem = "";
}

static void run_job(const Job &j) {
  Result r;
  r.op = j.op;
  JsonDocument doc, body;
  String b, path;
  int code;
  if (j.op == Op::Member) {
    body["card"] = j.a;
    serializeJson(body, b);
    code = call("POST", "/api/kiosk/member", b, doc, nullptr, 15000);
    if (code == 200) {
      JsonObjectConst m = doc["member"];
      serializeJson(m["id"], r.member.id_json);
      r.member.name = m["name"] | "";
      r.member.code = m["code"] | "";
      parse_loans(m["loans"], r.member.loans);
    }
  } else if (j.op == Op::Scan) {
    b = String("{\"kind\":\"") + j.a + "\",\"member_id\":" + (j.b.length() ? j.b : "null") + "}";
    // the server waits for the camera station before answering
    code = call("POST", "/api/kiosk/scan", b, doc, &scan_filter, 45000);
    if (code == 200) parse_scan(doc["scan"], r.scan);
  } else {
    path = "/api/kiosk/scan/" + j.a + "/confirm";
    code = call("POST", path.c_str(), "{}", doc, &scan_filter, 20000);
    if (code == 200) parse_scan(doc["scan"], r.scan);
  }
  r.ok = code == 200;
  if (!r.ok) r.error = err_text(code, doc);
  Serial.printf("[api] op %d -> %d %s\n", (int)j.op, code, r.error.c_str());
  Lock l;
  result = r;
  result_ready = true;
  job = Job();
}

static void worker(void *) {
  WiFi.persistent(false);  // the saved network lives in our own NVS keys, not the WiFi driver's
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);
  WiFi.setAutoReconnect(true);
  WiFi.onEvent([](arduino_event_id_t, arduino_event_info_t info) { last_reason = info.wifi_sta_disconnected.reason; },
               ARDUINO_EVENT_WIFI_STA_DISCONNECTED);
  load_wifi();
  begin_saved();
  configTzTime("ICT-7", "pool.ntp.org", "time.cloudflare.com");
  tls.setCACert(CA_ROOTS);  // used whenever the address is https://
  tls.setHandshakeTimeout(15);
  uint32_t last_poll = 0;
  bool was_up = false;
  for (;;) {
    wifi_service();
    if (WiFi.status() != WL_CONNECTED) {
      if (was_up) Serial.println("[api] WiFi lost");
      was_up = false;
      {
        Lock l;
        st.online = false;
        problem = saved_ssid.length() ? "กำลังเชื่อมต่อ Wi-Fi…" : "ยังไม่ได้ตั้งค่า Wi-Fi";
        if (job.op != Op::None) {  // fail the waiting request instead of leaving the UI busy
          result = Result();
          result.op = job.op;
          result.error = "Wi-Fi หลุด ลองใหม่อีกครั้ง";
          result_ready = true;
          job = Job();
        }
      }
      ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(500));  // a WiFi request from the screen wakes it early
      continue;
    }
    if (!was_up) {
      was_up = true;
      Serial.printf("[api] WiFi up, IP %s RSSI %d\n", WiFi.localIP().toString().c_str(), WiFi.RSSI());
      // No server chosen on the screen yet: on the laptop's Windows hotspot (gateway 192.168.137.1) use the
      // KitCheck running on that laptop (demo / virtual tray), anywhere else the secrets.h server.
      if (!prefs.isKey("base")) {
        String want = WiFi.gatewayIP() == IPAddress(192, 168, 137, 1) ? String("http://192.168.137.1:8080") : String(KC_BASE);
        if (want != base) {
          base = want;
          prefs.remove("token");
          token = "";
          drop_connections();
          Lock l;
          base_shared = base;
          token_shared = "";
        }
        Serial.printf("[api] server (auto) %s\n", base.c_str());
      }
      for (int i = 0; i < 50 && time(nullptr) < 1700000000; i++) vTaskDelay(pdMS_TO_TICKS(100));
    }

    static uint32_t last_log = 0;
    if (millis() - last_log > 10000) {
      last_log = millis();
      Serial.printf("[api] rssi %d, int free %u (min %u), psram free %u KB, cam %s, problem '%s'\n", WiFi.RSSI(),
                    (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL),
                    (unsigned)heap_caps_get_minimum_free_size(MALLOC_CAP_INTERNAL),
                    (unsigned)(heap_caps_get_free_size(MALLOC_CAP_SPIRAM) / 1024), "-",
                    api_problem().c_str());
    }

    Job j;
    String cancel;
    {
      Lock l;
      j = job;
      cancel = cancel_id;
      cancel_id = "";
    }
    if (cancel.length()) {
      JsonDocument doc;
      String p = "/api/kiosk/scan/" + cancel + "/cancel";
      call("POST", p.c_str(), "{}", doc, nullptr, 10000);
    }
    if (j.op != Op::None) {
      run_job(j);
      last_poll = 0;  // refresh the tray right after an action
      continue;
    }
    if (millis() - last_poll >= POLL_MS) {
      last_poll = millis();
      poll_state();
    }
    ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(30));
  }
}

void api_begin() {
  mu = xSemaphoreCreateMutex();
  build_filters();
  prefs.begin("kitcheck", false);
  base = prefs.getString("base", KC_BASE);
  base_shared = base;
  token = prefs.getString("token", "");
  token_shared = token;
  // core 0 next to the WiFi stack; LVGL runs in loop() on core 1
  xTaskCreatePinnedToCore(worker, "kitcheck-api", 12288, nullptr, 3, &worker_task, 0);
}
