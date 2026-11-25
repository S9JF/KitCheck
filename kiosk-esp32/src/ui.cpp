#include "ui.h"

#include <Arduino.h>
#include <lvgl.h>
#include <math.h>
#include <time.h>

#include "api.h"
#include "board.h"
#include "cam.h"
#include "font_thai_bold.h"
#include "font_thai_regular.h"
#include "secrets.h"

// Staff PIN for the WiFi settings. Set your own in secrets.h:  #define KIOSK_PIN "xxxx"
#ifndef KIOSK_PIN
#define KIOSK_PIN "1234"
#endif

// ---------- look (same palette as the web kiosk) ----------
static constexpr uint32_t C_BG = 0xF4F3EF, C_PANEL = 0xFFFFFF, C_LINE = 0xE4E2DC, C_INK = 0x1D1F23,
                          C_MUTED = 0x5F636A, C_HDR = 0x1D1F23, C_HDR_MUTED = 0xA3A6AD, C_PRI = 0xC2410C,
                          C_OK = 0x1F7A3F, C_OK_BG = 0xE4F2E8, C_WA = 0x7A4300, C_WA_BG = 0xFBEED6,
                          C_BD = 0xB42318, C_BD_BG = 0xFDECEA, C_INFO = 0x3F3D9E, C_INFO_BG = 0xECEBFA,
                          C_NEUTRAL_BG = 0xECEBE6, C_NEUTRAL = 0x45494F;

static lv_font_t *R18, *R22, *B22, *B28, *B40, *B64;

static uint32_t type_color(const String &t) {
  if (t == "hmr") return 0x7B5A3C;
  if (t == "plr") return 0xA3362B;
  if (t == "scr") return 0xC8901A;
  if (t == "drl") return 0x2F5F9E;
  if (t == "wrn") return 0x7D838C;
  return 0x9AA0A8;
}

// ---------- text helpers ----------
static String fmt_g(float g) {
  if (isnan(g)) return "—";
  long n = lroundf(g);
  bool neg = n < 0;
  String d = String(labs(n)), out;
  for (int i = 0; i < (int)d.length(); i++) {
    if (i && (d.length() - i) % 3 == 0) out += ',';
    out += d[i];
  }
  return (neg ? "-" : "") + out + " g";
}

static String fmt_date(double ms) {
  static const char *M[] = {"ม.ค.", "ก.พ.", "มี.ค.", "เม.ย.", "พ.ค.", "มิ.ย.", "ก.ค.", "ส.ค.", "ก.ย.", "ต.ค.", "พ.ย.", "ธ.ค."};
  static const char *D[] = {"อาทิตย์", "จันทร์", "อังคาร", "พุธ", "พฤหัสบดี", "ศุกร์", "เสาร์"};
  if (ms <= 0) return "—";
  time_t t = (time_t)(ms / 1000);
  struct tm tm;
  localtime_r(&t, &tm);
  return String(D[tm.tm_wday]) + " " + String(tm.tm_mday) + " " + M[tm.tm_mon] + " " + String(tm.tm_year + 1900 + 543);
}

static String clock_text() {
  time_t t = time(nullptr);
  if (t < 1700000000) return "--:--";
  struct tm tm;
  localtime_r(&t, &tm);
  char b[8];
  snprintf(b, sizeof b, "%02d:%02d", tm.tm_hour, tm.tm_min);
  return b;
}

// ---------- kiosk state ----------
enum class Scr { Idle, Keypad, Borrow, Confirm, Done, Returning, RetConfirm, Returned,
                 Offline, Pin, WifiList, WifiPass, WifiOk, Server };
static struct {
  // WiFi settings
  WifiInfo W;                 // latest from the network task
  String pin;                 // staff PIN entry
  std::vector<WifiNet> nets;  // last scan
  bool scanning = false;
  WifiNet sel;                // the network being joined
  bool hidden = false;        // typing the name of a hidden network
  bool show_pass = false;
  bool server_ok = false;
  String wifi_err;

  Scr screen = Scr::Idle;
  bool has_member = false;
  Member member;
  bool has_scan = false;
  Scan scan;
  String err;
  bool busy = false;
  bool armed = true;  // a return scan fires only after the tray has been empty once
  float last_scan_g = NAN;
  uint32_t count_at = 0, idle_at = 0;
  String code;  // keypad entry
} K;
static KioskState S;

// widgets
static lv_obj_t *hdr_left, *lbl_status, *lbl_sim, *lbl_tray, *lbl_clock, *body, *overlay, *overlay_lbl;
static lv_obj_t *live_weight, *live_pill, *count_arc, *count_lbl, *code_lbl;
static lv_obj_t *cam_img, *cam_msg, *cam_layer;  // camera view of the current screen (or null)
static const lv_image_dsc_t *cam_frame;           // latest frame taken from cam.cpp
static std::vector<CamBox> cam_boxes;
static int pill_kind = -1, status_kind = -1, chip_kind = -1;
static lv_obj_t *wifi_chip_lbl, *ta_ssid, *ta_pass, *kb, *wifi_msg, *eye_lbl;

// Only touch a label when its text really changes: every set_text redraws that area.
static void set_text(lv_obj_t *l, const char *s) {
  if (strcmp(lv_label_get_text(l), s) != 0) lv_label_set_text(l, s);
}

static void render();

static void go(Scr s) {
  K.screen = s;
  K.err = "";
  K.wifi_err = "";
  K.idle_at = millis();
  render();
}
static void reset() {
  K.has_member = false;
  K.has_scan = false;
  K.last_scan_g = NAN;
  go(Scr::Idle);
}
static bool on_tray() { return S.fresh && !isnan(S.grams) && S.grams > S.empty_g; }

// ---------- actions ----------
static void lookup() {
  if (K.busy || !K.code.length()) return;
  if (!api_member(K.code)) return;
  K.busy = true;
  K.err = "";
  render();
}
static void start_scan(const char *kind) {
  if (K.busy) return;
  bool ret = strcmp(kind, "return") == 0;
  if (!api_scan(kind, (!ret && K.has_member) ? K.member.id_json : String())) return;
  K.busy = true;
  K.armed = false;
  K.err = "";
  K.last_scan_g = S.grams;
  if (ret) K.screen = Scr::Returning;
  render();
}
static void start_countdown() {
  K.count_at = millis();
  go(Scr::Confirm);
}
static void confirm_scan() {
  if (K.busy || !K.has_scan) return;
  if (!api_confirm(K.scan.id)) return;
  K.busy = true;
  render();
}
static void cancel_round() {
  if (K.has_scan && K.scan.status != "done") api_cancel(K.scan.id);
  reset();
}

static void on_result(const Result &r) {
  K.busy = false;
  if (r.op == Op::Member) {
    if (r.ok) {
      K.member = r.member;
      K.has_member = true;
      K.has_scan = false;
      K.last_scan_g = NAN;
      go(Scr::Borrow);
    } else {
      K.err = r.error;
      render();
    }
  } else if (r.op == Op::Scan) {
    bool borrow = K.screen != Scr::Returning;
    if (r.ok) {
      K.scan = r.scan;
      K.has_scan = true;
      K.idle_at = millis();
      if (borrow) {
        if (r.scan.status == "ready") start_countdown();
        else go(Scr::Borrow);
      } else {
        go(r.scan.status == "ready" ? Scr::RetConfirm : Scr::Returned);
      }
    } else {
      if (!borrow) K.screen = Scr::Idle;
      K.err = r.error;
      render();
    }
  } else if (r.op == Op::Confirm) {
    bool borrow = K.scan.kind == "borrow";
    if (r.ok) {
      K.scan = r.scan;
      go(borrow ? Scr::Done : Scr::Returned);
    } else if (borrow) {
      go(Scr::Borrow);
      K.err = r.error;
      render();
    } else {
      K.err = r.error;
      render();
    }
  }
}

// ---------- WiFi actions ----------
static void show_busy(const String &msg) {
  lv_label_set_text(overlay_lbl, msg.c_str());
  lv_obj_remove_flag(overlay, LV_OBJ_FLAG_HIDDEN);
}
static void wifi_scan_now() {
  if (K.scanning) return;
  K.scanning = true;
  wifi_scan();
}
static void join(const String &ssid, const String &pass) {
  K.busy = true;
  K.wifi_err = "";
  K.sel.ssid = ssid;
  wifi_connect(ssid, pass);
  show_busy("กำลังเชื่อมต่อ " + ssid + "…");
}
static void wifi_msg_set(const String &s, bool bad) {
  if (!wifi_msg) return;
  lv_label_set_text(wifi_msg, s.c_str());
  lv_obj_set_style_text_color(wifi_msg, lv_color_hex(bad ? C_BD : C_MUTED), 0);
}
static void wifi_try() {
  if (K.busy || K.screen != Scr::WifiPass || !ta_pass) return;
  String ssid = K.hidden ? String(lv_textarea_get_text(ta_ssid)) : K.sel.ssid;
  String pass = lv_textarea_get_text(ta_pass);
  if (K.hidden) ssid.trim();
  if (!ssid.length()) return wifi_msg_set("ใส่ชื่อ WiFi ก่อน", true);
  bool need_pass = K.hidden ? pass.length() > 0 : K.sel.locked;  // hidden + empty password = open network
  if (need_pass && pass.length() < 8) return wifi_msg_set("รหัสผ่าน WiFi ต้องมีอย่างน้อย 8 ตัว", true);
  join(ssid, pass);
}
static void pick_net(const WifiNet &n) {
  if (K.busy) return;
  if (n.enterprise) {
    K.wifi_err = n.ssid + " ต้องใช้บัญชีองค์กร (Enterprise) ตู้นี้ยังไม่รองรับ";
    render();
    return;
  }
  K.sel = n;
  K.hidden = false;
  K.show_pass = false;
  if (!n.locked) join(n.ssid, "");
  else go(Scr::WifiPass);
}

static void server_save();
enum Act { A_KEYPAD, A_RETURN, A_SCAN, A_CONFIRM, A_CANCEL, A_HOME,
           A_WIFI, A_WIFI_SCAN, A_WIFI_HIDDEN, A_WIFI_FORGET, A_WIFI_BACK, A_WIFI_CONNECT, A_WIFI_EYE,
           A_SERVER, A_SERVER_SAVE, A_PRESET_LAPTOP, A_PRESET_REAL };
static void act_cb(lv_event_t *e) {
  switch ((intptr_t)lv_event_get_user_data(e)) {
    case A_KEYPAD: K.code = ""; go(Scr::Keypad); break;
    case A_RETURN: start_scan("return"); break;
    case A_SCAN: start_scan("borrow"); break;
    case A_CONFIRM: confirm_scan(); break;
    case A_CANCEL: cancel_round(); break;
    case A_HOME: reset(); break;
    case A_WIFI:  // only from the home / no-WiFi screen, never in the middle of a borrow or return
      if (K.busy || (K.screen != Scr::Idle && K.screen != Scr::Offline)) break;
      K.pin = "";
      go(Scr::Pin);
      break;
    case A_WIFI_SCAN: wifi_scan_now(); render(); break;
    case A_WIFI_HIDDEN:
      K.sel = WifiNet();
      K.sel.rssi = -60;
      K.hidden = true;
      K.show_pass = false;
      go(Scr::WifiPass);
      break;
    case A_WIFI_FORGET:
      wifi_forget();
      K.W.connected = false;
      K.W.has_saved = false;
      K.W.ssid = "";
      render();
      break;
    case A_WIFI_BACK: go(Scr::WifiList); break;
    case A_WIFI_CONNECT: wifi_try(); break;
    case A_WIFI_EYE:
      K.show_pass = !K.show_pass;
      if (ta_pass) lv_textarea_set_password_mode(ta_pass, !K.show_pass);
      if (eye_lbl) lv_label_set_text(eye_lbl, K.show_pass ? LV_SYMBOL_EYE_CLOSE : LV_SYMBOL_EYE_OPEN);
      break;
    case A_SERVER: go(Scr::Server); break;
    case A_SERVER_SAVE: server_save(); break;
    case A_PRESET_LAPTOP:
      if (ta_ssid) lv_textarea_set_text(ta_ssid, "http://192.168.137.1:8080");
      break;
    case A_PRESET_REAL:
      if (ta_ssid) lv_textarea_set_text(ta_ssid, KC_BASE);
      break;
  }
}

// ---------- widget helpers ----------
static lv_obj_t *box(lv_obj_t *p, lv_flex_flow_t flow, int gap = 12) {
  lv_obj_t *o = lv_obj_create(p);
  lv_obj_remove_style_all(o);
  lv_obj_set_flex_flow(o, flow);
  lv_obj_set_style_pad_row(o, gap, 0);
  lv_obj_set_style_pad_column(o, gap, 0);
  lv_obj_remove_flag(o, LV_OBJ_FLAG_SCROLLABLE);
  lv_obj_set_size(o, LV_PCT(100), LV_SIZE_CONTENT);
  return o;
}
static lv_obj_t *panel(lv_obj_t *p, int pad = 22) {
  lv_obj_t *o = box(p, LV_FLEX_FLOW_COLUMN, 10);
  lv_obj_set_style_bg_color(o, lv_color_hex(C_PANEL), 0);
  lv_obj_set_style_bg_opa(o, LV_OPA_COVER, 0);
  lv_obj_set_style_radius(o, 18, 0);
  lv_obj_set_style_border_width(o, 1, 0);
  lv_obj_set_style_border_color(o, lv_color_hex(C_LINE), 0);
  lv_obj_set_style_pad_all(o, pad, 0);
  return o;
}
static lv_obj_t *banner(lv_obj_t *p, uint32_t bg) {
  lv_obj_t *o = panel(p, 16);
  lv_obj_set_style_bg_color(o, lv_color_hex(bg), 0);
  lv_obj_set_style_border_width(o, 0, 0);
  lv_obj_set_style_radius(o, 14, 0);
  lv_obj_set_style_pad_row(o, 4, 0);
  return o;
}
static lv_obj_t *text(lv_obj_t *p, const String &s, const lv_font_t *f, uint32_t color = C_INK, bool wrap = true) {
  lv_obj_t *l = lv_label_create(p);
  lv_label_set_text(l, s.c_str());
  lv_obj_set_style_text_font(l, f, 0);
  lv_obj_set_style_text_color(l, lv_color_hex(color), 0);
  if (wrap) {
    lv_label_set_long_mode(l, LV_LABEL_LONG_WRAP);
    lv_obj_set_width(l, LV_PCT(100));
  }
  return l;
}
static void spacer(lv_obj_t *p) {
  lv_obj_t *o = lv_obj_create(p);
  lv_obj_remove_style_all(o);
  lv_obj_set_width(o, 1);
  lv_obj_set_flex_grow(o, 1);
}
static lv_obj_t *button(lv_obj_t *p, const String &s, uint32_t bg, uint32_t fg, Act a, int h = 76) {
  lv_obj_t *b = lv_button_create(p);
  lv_obj_set_size(b, LV_PCT(100), h);
  lv_obj_set_style_bg_color(b, lv_color_hex(bg), 0);
  lv_obj_set_style_radius(b, 16, 0);
  lv_obj_set_style_shadow_width(b, 0, 0);
  if (bg == C_PANEL) {
    lv_obj_set_style_border_width(b, 2, 0);
    lv_obj_set_style_border_color(b, lv_color_hex(C_LINE), 0);
  }
  lv_obj_t *l = lv_label_create(b);
  lv_label_set_text(l, s.c_str());
  lv_obj_set_style_text_font(l, B28, 0);
  lv_obj_set_style_text_color(l, lv_color_hex(fg), 0);
  lv_obj_center(l);
  lv_obj_add_event_cb(b, act_cb, LV_EVENT_CLICKED, (void *)(intptr_t)a);
  return b;
}
static lv_obj_t *icon_tile(lv_obj_t *p, const char *sym, uint32_t bg, uint32_t fg) {
  lv_obj_t *o = lv_obj_create(p);
  lv_obj_remove_style_all(o);
  lv_obj_set_size(o, 88, 88);
  lv_obj_set_style_radius(o, 22, 0);
  lv_obj_set_style_bg_color(o, lv_color_hex(bg), 0);
  lv_obj_set_style_bg_opa(o, LV_OPA_COVER, 0);
  lv_obj_t *l = lv_label_create(o);
  lv_label_set_text(l, sym);
  lv_obj_set_style_text_font(l, &lv_font_montserrat_48, 0);
  lv_obj_set_style_text_color(l, lv_color_hex(fg), 0);
  lv_obj_center(l);
  return o;
}
static lv_obj_t *err_label(lv_obj_t *p) {
  if (!K.err.length()) return nullptr;
  return text(p, K.err, B22, C_BD);
}
// two columns that share the body 3:2, or camera-wide left column + the rest
static void columns(lv_obj_t **left, lv_obj_t **right, bool cam = false) {
  lv_obj_t *row = box(body, LV_FLEX_FLOW_ROW, 20);
  lv_obj_set_size(row, LV_PCT(100), LV_PCT(100));
  *left = box(row, LV_FLEX_FLOW_COLUMN, 12);
  lv_obj_set_height(*left, LV_PCT(100));
  if (cam) lv_obj_set_width(*left, CAM_W);
  else lv_obj_set_flex_grow(*left, 3);
  *right = box(row, LV_FLEX_FLOW_COLUMN, 12);
  lv_obj_set_height(*right, LV_PCT(100));
  lv_obj_set_flex_grow(*right, 2);
}
static lv_obj_t *scroll_panel(lv_obj_t *p) {
  lv_obj_t *o = panel(p);
  lv_obj_set_flex_grow(o, 1);
  lv_obj_add_flag(o, LV_OBJ_FLAG_SCROLLABLE);
  lv_obj_set_scroll_dir(o, LV_DIR_VER);
  return o;
}

// ---------- live camera ----------
static void cam_show_frame() {
  if (!cam_img || !cam_frame) return;
  lv_image_cache_drop(cam_frame);
  lv_image_set_src(cam_img, cam_frame);
  lv_obj_invalidate(cam_img);
  lv_obj_clean(cam_layer);
  for (auto &b : cam_boxes) {
    uint32_t c = type_color(b.type);
    lv_obj_t *r = lv_obj_create(cam_layer);
    lv_obj_remove_style_all(r);
    lv_obj_set_pos(r, b.x, b.y);
    lv_obj_set_size(r, b.w, b.h);
    lv_obj_set_style_border_width(r, 3, 0);
    lv_obj_set_style_border_color(r, lv_color_hex(c), 0);
    lv_obj_set_style_radius(r, 6, 0);
    lv_obj_t *t = lv_label_create(cam_layer);
    lv_label_set_text(t, b.label.c_str());
    lv_obj_set_style_text_font(t, R18, 0);
    lv_obj_set_style_text_color(t, lv_color_hex(0xFFFFFF), 0);
    lv_obj_set_style_bg_color(t, lv_color_hex(c), 0);
    lv_obj_set_style_bg_opa(t, LV_OPA_COVER, 0);
    lv_obj_set_style_pad_hor(t, 6, 0);
    lv_obj_set_pos(t, max(0, b.x), max(0, b.y - 26));
  }
  lv_obj_add_flag(cam_msg, LV_OBJ_FLAG_HIDDEN);
}

static void camera_view(lv_obj_t *p) {
  lv_obj_t *v = lv_obj_create(p);
  lv_obj_remove_style_all(v);
  lv_obj_set_size(v, CAM_W, CAM_H);
  lv_obj_set_style_bg_color(v, lv_color_hex(0x1C1E21), 0);
  lv_obj_set_style_bg_opa(v, LV_OPA_COVER, 0);
  lv_obj_set_style_radius(v, 16, 0);
  lv_obj_set_style_clip_corner(v, true, 0);
  lv_obj_remove_flag(v, LV_OBJ_FLAG_SCROLLABLE);
  cam_img = lv_image_create(v);
  lv_obj_set_pos(cam_img, 0, 0);
  cam_layer = lv_obj_create(v);
  lv_obj_remove_style_all(cam_layer);
  lv_obj_set_size(cam_layer, CAM_W, CAM_H);
  lv_obj_remove_flag(cam_layer, LV_OBJ_FLAG_SCROLLABLE);
  cam_msg = text(v, "กำลังเปิดกล้อง…", R18, 0xA3A6AD, false);
  lv_obj_center(cam_msg);
  cam_show_frame();
}

static void det_row(lv_obj_t *p, const Det &d, bool show_w, const String &right = String()) {
  lv_obj_t *row = box(p, LV_FLEX_FLOW_ROW, 14);
  lv_obj_set_flex_align(row, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
  lv_obj_set_style_pad_ver(row, 8, 0);
  lv_obj_set_style_border_side(row, LV_BORDER_SIDE_BOTTOM, 0);
  lv_obj_set_style_border_width(row, 1, 0);
  lv_obj_set_style_border_color(row, lv_color_hex(C_LINE), 0);
  lv_obj_t *sw = lv_obj_create(row);
  lv_obj_remove_style_all(sw);
  lv_obj_set_size(sw, 14, 44);
  lv_obj_set_style_radius(sw, 4, 0);
  lv_obj_set_style_bg_color(sw, lv_color_hex(type_color(d.type)), 0);
  lv_obj_set_style_bg_opa(sw, LV_OPA_COVER, 0);
  lv_obj_t *col = box(row, LV_FLEX_FLOW_COLUMN, 0);
  lv_obj_set_width(col, 10);
  lv_obj_set_flex_grow(col, 1);
  text(col, d.type_name, B22);
  text(col, d.qr.length() ? d.qr : String("อ่าน QR ไม่ได้"), R18, C_MUTED);
  if (d.issue_th.length()) {
    String s = d.issue_th;
    if (d.issue == "out" && d.owner.length()) s += " · อยู่กับ " + d.owner;
    text(col, s, R18, d.issue == "unread" ? C_WA : C_BD);
  }
  if (right.length()) text(row, right, R18, C_MUTED, false);
  else if (show_w && !isnan(d.weight)) text(row, fmt_g(d.weight), R18, C_MUTED, false);
  if (!right.length()) {
    lv_obj_t *mark = text(row, d.issue.length() ? "!" : LV_SYMBOL_OK, B28, d.issue.length() ? C_BD : C_OK, false);
    lv_obj_set_style_min_width(mark, 28, 0);
  }
}

static String weight_line(const Scan &sc, bool tick) {
  if (isnan(sc.measured_g)) return "";
  String s = "น้ำหนักบนถาด " + fmt_g(sc.measured_g);
  if (!isnan(sc.expected_g)) s += " · ตามทะเบียน " + fmt_g(sc.expected_g) + (tick ? " " LV_SYMBOL_OK : "");
  return s;
}

// ---------- screens ----------
static void v_idle() {
  lv_obj_t *col = box(body, LV_FLEX_FLOW_COLUMN, 16);
  lv_obj_set_size(col, LV_PCT(100), LV_PCT(100));
  lv_obj_t *row = box(col, LV_FLEX_FLOW_ROW, 24);
  lv_obj_set_flex_grow(row, 1);

  lv_obj_t *l = panel(row, 26);
  lv_obj_set_height(l, LV_PCT(100));
  lv_obj_set_flex_grow(l, 1);
  icon_tile(l, LV_SYMBOL_KEYBOARD, 0xFFF1E8, C_PRI);
  text(l, "ยืม", B40);
  text(l, "ใส่รหัสสมาชิก แล้ววางเครื่องมือที่ต้องการบนถาด", R22);
  text(l, "กี่ชิ้นก็ได้ ระบบตรวจเองเมื่อวางเสร็จ", R18, C_MUTED);
  spacer(l);
  err_label(l);
  button(l, "ยืม · ใส่รหัสสมาชิก", C_PRI, 0xFFFFFF, A_KEYPAD);

  lv_obj_t *r = panel(row, 26);
  lv_obj_set_height(r, LV_PCT(100));
  lv_obj_set_flex_grow(r, 1);
  icon_tile(r, LV_SYMBOL_DOWNLOAD, C_OK_BG, C_OK);
  text(r, "คืน", B40);
  text(r, "วางเครื่องมือบนถาดได้เลย ไม่ต้องใส่รหัส", R22);
  text(r, "ระบบอ่าน QR แล้วรู้เองว่าแต่ละชิ้นเป็นของใคร คืนไม่ครบก็ได้", R18, C_MUTED);
  spacer(r);
  button(r, "วางของแล้ว · ตรวจคืน", C_OK, 0xFFFFFF, A_RETURN);

  lv_obj_t *foot = box(col, LV_FLEX_FLOW_ROW, 40);
  lv_obj_set_flex_align(foot, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
  for (const char *s : {"1  หันป้าย QR ขึ้นด้านบน", "2  วางให้แยกกัน ไม่ซ้อน", "3  ปล่อยมือจากถาด"}) text(foot, s, R18, C_NEUTRAL, false);
}

static lv_obj_t *numpad(lv_obj_t *row, lv_event_cb_t cb);
static String short_base(const String &b);
static lv_obj_t *big_mark(lv_obj_t *p, uint32_t bg, const char *sym);

static const char *KP_MAP[] = {"1", "2", "3", "\n", "4", "5", "6", "\n", "7", "8", "9", "\n", LV_SYMBOL_BACKSPACE, "0", "ตกลง", ""};
static void keypad_cb(lv_event_t *e) {
  lv_obj_t *m = (lv_obj_t *)lv_event_get_target(e);
  uint32_t id = lv_buttonmatrix_get_selected_button(m);
  if (id == LV_BUTTONMATRIX_BUTTON_NONE) return;
  K.idle_at = millis();
  const char *t = lv_buttonmatrix_get_button_text(m, id);
  if (strcmp(t, LV_SYMBOL_BACKSPACE) == 0) {
    if (K.code.length()) K.code.remove(K.code.length() - 1);
  } else if (strcmp(t, "ตกลง") == 0) {
    lookup();
    return;
  } else if (K.code.length() < 16) {
    K.code += t;
  }
  if (K.err.length()) {  // typing again clears the last "not found"
    K.err = "";
    render();
    return;
  }
  if (code_lbl) lv_label_set_text(code_lbl, K.code.length() ? K.code.c_str() : " ");
}
static void v_keypad() {
  lv_obj_t *row = box(body, LV_FLEX_FLOW_ROW, 24);
  lv_obj_set_size(row, LV_PCT(100), LV_PCT(100));
  lv_obj_t *l = panel(row, 26);
  lv_obj_set_size(l, 400, LV_PCT(100));
  text(l, "ใส่รหัสสมาชิก", B28);
  text(l, "รหัสนักศึกษา หรือรหัสสมาชิกที่ลงทะเบียนไว้", R18, C_MUTED);
  lv_obj_t *field = panel(l, 14);
  lv_obj_set_style_border_width(field, 2, 0);
  lv_obj_set_style_border_color(field, lv_color_hex(C_PRI), 0);
  lv_obj_set_style_min_height(field, 84, 0);
  lv_obj_set_flex_align(field, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_START);
  code_lbl = text(field, K.code.length() ? K.code : String(" "), B40);
  err_label(l);
  spacer(l);
  button(l, "ยกเลิก", C_PANEL, C_INK, A_HOME);
  numpad(row, keypad_cb);
}

static lv_obj_t *numpad(lv_obj_t *row, lv_event_cb_t cb) {
  lv_obj_t *m = lv_buttonmatrix_create(row);
  lv_buttonmatrix_set_map(m, KP_MAP);
  lv_buttonmatrix_set_button_ctrl(m, 11, LV_BUTTONMATRIX_CTRL_CHECKED);
  lv_obj_set_height(m, LV_PCT(100));
  lv_obj_set_flex_grow(m, 1);
  lv_obj_set_style_bg_opa(m, LV_OPA_TRANSP, 0);
  lv_obj_set_style_border_width(m, 0, 0);
  lv_obj_set_style_pad_all(m, 0, 0);
  lv_obj_set_style_pad_gap(m, 12, 0);
  lv_obj_set_style_text_font(m, B40, LV_PART_ITEMS);
  lv_obj_set_style_radius(m, 16, LV_PART_ITEMS);
  lv_obj_set_style_bg_color(m, lv_color_hex(C_PANEL), LV_PART_ITEMS);
  lv_obj_set_style_border_width(m, 1, LV_PART_ITEMS);
  lv_obj_set_style_border_color(m, lv_color_hex(C_LINE), LV_PART_ITEMS);
  lv_obj_set_style_shadow_width(m, 0, LV_PART_ITEMS);
  lv_obj_set_style_text_color(m, lv_color_hex(C_INK), LV_PART_ITEMS);
  lv_obj_set_style_bg_color(m, lv_color_hex(C_PRI), LV_PART_ITEMS | LV_STATE_CHECKED);
  lv_obj_set_style_text_color(m, lv_color_hex(0xFFFFFF), LV_PART_ITEMS | LV_STATE_CHECKED);
  lv_obj_set_style_bg_color(m, lv_color_hex(C_NEUTRAL_BG), LV_PART_ITEMS | LV_STATE_PRESSED);
  lv_obj_add_event_cb(m, cb, LV_EVENT_VALUE_CHANGED, nullptr);
  return m;
}

// ---------- WiFi screens ----------
static const char *QUAL[] = {"", "อ่อน", "พอใช้", "ดี", "ดีมาก"};
static String quality(int rssi) { return String(QUAL[wifi_level(rssi)]) + " (" + String(rssi) + " dBm)"; }

static String pin_dots() {
  String s;
  for (size_t i = 0; i < K.pin.length(); i++) s += LV_SYMBOL_BULLET " ";
  return s.length() ? s : String(" ");
}
static void pin_cb(lv_event_t *e) {
  lv_obj_t *m = (lv_obj_t *)lv_event_get_target(e);
  uint32_t id = lv_buttonmatrix_get_selected_button(m);
  if (id == LV_BUTTONMATRIX_BUTTON_NONE) return;
  K.idle_at = millis();
  const char *t = lv_buttonmatrix_get_button_text(m, id);
  if (strcmp(t, LV_SYMBOL_BACKSPACE) == 0) {
    if (K.pin.length()) K.pin.remove(K.pin.length() - 1);
  } else if (strcmp(t, "ตกลง") == 0) {
    bool ok = K.pin == KIOSK_PIN;
    K.pin = "";
    if (ok) {
      wifi_scan_now();
      go(Scr::WifiList);
    } else {
      K.err = "รหัสไม่ถูกต้อง ลองใหม่";
      render();
    }
    return;
  } else if (K.pin.length() < 8) {
    K.pin += t;
  }
  if (K.err.length()) {
    K.err = "";
    render();
    return;
  }
  if (code_lbl) lv_label_set_text(code_lbl, pin_dots().c_str());
}
static void v_pin() {
  lv_obj_t *row = box(body, LV_FLEX_FLOW_ROW, 24);
  lv_obj_set_size(row, LV_PCT(100), LV_PCT(100));
  lv_obj_t *l = panel(row, 26);
  lv_obj_set_size(l, 400, LV_PCT(100));
  text(l, "รหัสเจ้าหน้าที่", B28);
  text(l, "เปลี่ยน WiFi ได้เฉพาะเจ้าหน้าที่ เพื่อไม่ให้ใครมาย้ายตู้ไปต่อเครือข่ายอื่น", R18, C_MUTED);
  lv_obj_t *field = panel(l, 14);
  lv_obj_set_style_border_width(field, 2, 0);
  lv_obj_set_style_border_color(field, lv_color_hex(C_PRI), 0);
  lv_obj_set_style_min_height(field, 84, 0);
  lv_obj_set_flex_align(field, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_START);
  code_lbl = text(field, pin_dots(), B40);
  err_label(l);
  spacer(l);
  button(l, "ยกเลิก", C_PANEL, C_INK, A_HOME);
  numpad(row, pin_cb);
}

static void v_offline() {
  lv_obj_t *col = box(body, LV_FLEX_FLOW_COLUMN, 14);
  lv_obj_set_size(col, LV_PCT(100), LV_PCT(100));
  lv_obj_set_flex_align(col, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
  big_mark(col, C_BD, LV_SYMBOL_WIFI);
  text(col, "ยังไม่ได้เชื่อมต่อ WiFi", B40, C_INK, false);
  text(col, "ตู้ต้องต่อ WiFi ก่อนถึงจะยืม–คืนได้ ให้เจ้าหน้าที่ตั้งค่า WiFi", R22, C_NEUTRAL, false);
  text(col, K.W.has_saved ? "กำลังลองต่อ " + K.W.ssid + " ใหม่เองอยู่เรื่อยๆ" : String("ยังไม่มี WiFi ที่บันทึกไว้ในตู้"),
       R18, C_MUTED, false);
  lv_obj_t *b = button(col, "ตั้งค่า WiFi", C_PRI, 0xFFFFFF, A_WIFI);
  lv_obj_set_width(b, 360);
}

// children of a list row must not swallow the row's click
static lv_obj_t *passthru(lv_obj_t *o) {
  lv_obj_remove_flag(o, LV_OBJ_FLAG_CLICKABLE);
  lv_obj_add_flag(o, LV_OBJ_FLAG_EVENT_BUBBLE);
  return o;
}
static void net_cb(lv_event_t *e) {
  size_t i = (size_t)(intptr_t)lv_event_get_user_data(e);
  K.idle_at = millis();
  if (i < K.nets.size()) pick_net(K.nets[i]);
}
static void net_row(lv_obj_t *p, const WifiNet &n, int i) {
  bool on = K.W.connected && K.W.ssid == n.ssid;
  int lv = wifi_level(n.rssi);
  lv_obj_t *b = lv_button_create(p);
  lv_obj_remove_style_all(b);
  lv_obj_set_size(b, LV_PCT(100), 68);
  lv_obj_set_flex_flow(b, LV_FLEX_FLOW_ROW);
  lv_obj_set_flex_align(b, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
  lv_obj_set_style_pad_column(b, 16, 0);
  lv_obj_set_style_pad_hor(b, 10, 0);
  lv_obj_set_style_radius(b, 10, 0);
  lv_obj_set_style_bg_color(b, lv_color_hex(on ? C_OK_BG : C_NEUTRAL_BG), 0);
  lv_obj_set_style_bg_opa(b, on ? LV_OPA_COVER : LV_OPA_TRANSP, 0);
  lv_obj_set_style_bg_opa(b, LV_OPA_COVER, LV_STATE_PRESSED);
  lv_obj_set_style_border_side(b, LV_BORDER_SIDE_BOTTOM, 0);
  lv_obj_set_style_border_width(b, 1, 0);
  lv_obj_set_style_border_color(b, lv_color_hex(C_LINE), 0);
  lv_obj_add_event_cb(b, net_cb, LV_EVENT_CLICKED, (void *)(intptr_t)i);

  lv_obj_t *bars = passthru(box(b, LV_FLEX_FLOW_ROW, 3));
  lv_obj_set_size(bars, 28, 22);
  lv_obj_set_flex_align(bars, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_END, LV_FLEX_ALIGN_END);
  for (int k = 1; k <= 4; k++) {
    lv_obj_t *bar = passthru(lv_obj_create(bars));
    lv_obj_remove_style_all(bar);
    lv_obj_set_size(bar, 5, 2 + 5 * k);
    lv_obj_set_style_radius(bar, 2, 0);
    lv_obj_set_style_bg_color(bar, lv_color_hex(k > lv ? 0xDAD7D0 : lv == 1 ? C_BD : C_INK), 0);
    lv_obj_set_style_bg_opa(bar, LV_OPA_COVER, 0);
  }
  lv_obj_t *col = passthru(box(b, LV_FLEX_FLOW_COLUMN, 0));
  lv_obj_set_width(col, 10);
  lv_obj_set_flex_grow(col, 1);
  text(col, n.ssid, B22, C_INK, false);
  String sub = on ? String("เชื่อมต่ออยู่")
             : n.enterprise ? String("บัญชีองค์กร (Enterprise) · ยังไม่รองรับ")
                            : String(n.locked ? "มีรหัสผ่าน" : "ไม่มีรหัสผ่าน") + " · สัญญาณ" + QUAL[lv];
  text(col, sub, R18, on ? C_OK : lv == 1 ? C_BD : C_MUTED, false);
  lv_obj_t *chev = text(b, LV_SYMBOL_RIGHT, R22, 0xA3A6AD, false);
  (void)chev;
}
static lv_obj_t *small_button(lv_obj_t *p, const String &s, uint32_t fg, Act a, bool outline) {
  lv_obj_t *b = lv_button_create(p);
  lv_obj_set_height(b, 52);
  lv_obj_set_style_pad_hor(b, 18, 0);
  lv_obj_set_style_radius(b, 14, 0);
  lv_obj_set_style_shadow_width(b, 0, 0);
  lv_obj_set_style_bg_color(b, lv_color_hex(C_PANEL), 0);
  lv_obj_set_style_bg_color(b, lv_color_hex(C_NEUTRAL_BG), LV_STATE_PRESSED);
  lv_obj_set_style_border_width(b, outline ? 2 : 0, 0);
  lv_obj_set_style_border_color(b, lv_color_hex(C_LINE), 0);
  lv_obj_t *l = lv_label_create(b);
  lv_label_set_text(l, s.c_str());
  lv_obj_set_style_text_font(l, R22, 0);
  lv_obj_set_style_text_color(l, lv_color_hex(fg), 0);
  lv_obj_center(l);
  lv_obj_add_event_cb(b, act_cb, LV_EVENT_CLICKED, (void *)(intptr_t)a);
  return b;
}
static void kv_row(lv_obj_t *p, const String &k, const String &v, uint32_t vc = C_INK, bool line = true) {
  lv_obj_t *r = box(p, LV_FLEX_FLOW_ROW, 8);
  lv_obj_set_flex_align(r, LV_FLEX_ALIGN_SPACE_BETWEEN, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
  lv_obj_set_style_pad_ver(r, 6, 0);
  if (line) {
    lv_obj_set_style_border_side(r, LV_BORDER_SIDE_BOTTOM, 0);
    lv_obj_set_style_border_width(r, 1, 0);
    lv_obj_set_style_border_color(r, lv_color_hex(C_LINE), 0);
  }
  text(r, k, R18, C_MUTED, false);
  text(r, v, R18, vc, false);
}

static void v_wifi_list() {
  lv_obj_t *l, *r;
  columns(&l, &r);
  lv_obj_t *p = panel(l, 18);
  lv_obj_set_flex_grow(p, 1);
  lv_obj_t *top = box(p, LV_FLEX_FLOW_ROW, 10);
  lv_obj_set_flex_align(top, LV_FLEX_ALIGN_SPACE_BETWEEN, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
  lv_obj_t *tc = box(top, LV_FLEX_FLOW_COLUMN, 0);
  lv_obj_set_width(tc, LV_SIZE_CONTENT);
  text(tc, "เลือก WiFi", B28, C_INK, false);
  text(tc, "ตู้นี้ต่อได้เฉพาะ WiFi 2.4 GHz", R18, C_MUTED, false);
  small_button(top, K.scanning ? String(LV_SYMBOL_REFRESH " กำลังค้นหา…") : String(LV_SYMBOL_REFRESH " ค้นหาใหม่"), C_INK,
               A_WIFI_SCAN, true);
  if (K.wifi_err.length()) {
    lv_obj_t *bn = banner(p, C_BD_BG);
    text(bn, K.wifi_err, B22, C_BD);
  }
  lv_obj_t *list = box(p, LV_FLEX_FLOW_COLUMN, 0);
  lv_obj_set_flex_grow(list, 1);
  lv_obj_add_flag(list, LV_OBJ_FLAG_SCROLLABLE);
  lv_obj_set_scroll_dir(list, LV_DIR_VER);
  if (!K.nets.size()) text(list, K.scanning ? "กำลังค้นหา WiFi รอบๆ ตู้…" : "ไม่พบ WiFi · กดค้นหาใหม่", R22, C_MUTED);
  for (size_t i = 0; i < K.nets.size(); i++) net_row(list, K.nets[i], (int)i);
  lv_obj_t *hb = small_button(list, LV_SYMBOL_PLUS "  เพิ่มเครือข่ายที่ซ่อนอยู่ (พิมพ์ชื่อเอง)", C_PRI, A_WIFI_HIDDEN, false);
  lv_obj_set_style_bg_opa(hb, LV_OPA_TRANSP, 0);
  lv_obj_set_height(hb, 60);

  lv_obj_t *info = panel(r);
  lv_obj_set_flex_grow(info, 1);
  text(info, "ตอนนี้", R18, C_MUTED);
  if (K.W.connected) {
    text(info, K.W.ssid, B28);
    lv_obj_t *pill = text(info, LV_SYMBOL_BULLET " เชื่อมต่ออยู่", R18, C_OK, false);
    lv_obj_set_style_bg_color(pill, lv_color_hex(C_OK_BG), 0);
    lv_obj_set_style_bg_opa(pill, LV_OPA_COVER, 0);
    lv_obj_set_style_radius(pill, 999, 0);
    lv_obj_set_style_pad_hor(pill, 12, 0);
    lv_obj_set_style_pad_ver(pill, 4, 0);
    kv_row(info, "สัญญาณ", quality(K.W.rssi));
    kv_row(info, "IP", K.W.ip);
    kv_row(info, "เซิร์ฟเวอร์", String(S.online ? LV_SYMBOL_OK " " : "") + short_base(api_base()),
           S.online ? C_OK : C_BD, false);
    spacer(info);
    small_button(info, "ลืมเครือข่ายนี้", C_BD, A_WIFI_FORGET, false);
  } else {
    text(info, "ยังไม่ได้เชื่อมต่อ", B28, C_BD);
    text(info, K.W.has_saved ? "กำลังลองต่อ " + K.W.ssid + " อยู่ หรือเลือก WiFi อื่นทางซ้าย"
                             : String("แตะชื่อ WiFi ทางซ้าย แล้วใส่รหัสผ่าน ตู้จะจำไว้ เปิดเครื่องครั้งหน้าต่อเองอัตโนมัติ"),
         R18, C_NEUTRAL);
    kv_row(info, "เซิร์ฟเวอร์", short_base(api_base()), C_INK, false);
  }
  lv_obj_t *br = box(r, LV_FLEX_FLOW_ROW, 12);
  lv_obj_t *b = button(br, "เซิร์ฟเวอร์", C_PANEL, C_INK, A_SERVER);
  lv_obj_set_width(b, 10);
  lv_obj_set_flex_grow(b, 1);
  b = button(br, "เสร็จ", C_PANEL, C_INK, A_HOME);
  lv_obj_set_width(b, 10);
  lv_obj_set_flex_grow(b, 1);
}

// on-screen keyboard: digits always on top (WiFi passwords mix them in), Thai not needed here
static const char *KB_LOWER[] = {"1", "2", "3", "4", "5", "6", "7", "8", "9", "0", "\n",
                                 "q", "w", "e", "r", "t", "y", "u", "i", "o", "p", "\n",
                                 "a", "s", "d", "f", "g", "h", "j", "k", "l", "\n",
                                 "ABC", "z", "x", "c", "v", "b", "n", "m", LV_SYMBOL_BACKSPACE, "\n",
                                 "1#", " ", ".", "-", "_", "@", LV_SYMBOL_OK, ""};
static const char *KB_UPPER[] = {"1", "2", "3", "4", "5", "6", "7", "8", "9", "0", "\n",
                                 "Q", "W", "E", "R", "T", "Y", "U", "I", "O", "P", "\n",
                                 "A", "S", "D", "F", "G", "H", "J", "K", "L", "\n",
                                 "abc", "Z", "X", "C", "V", "B", "N", "M", LV_SYMBOL_BACKSPACE, "\n",
                                 "1#", " ", ".", "-", "_", "@", LV_SYMBOL_OK, ""};
static const char *KB_SPECIAL[] = {"1", "2", "3", "4", "5", "6", "7", "8", "9", "0", "\n",
                                   "!", "@", "#", "$", "%", "^", "&", "*", "(", ")", "\n",
                                   "-", "_", "=", "+", "[", "]", "{", "}", ";", "\n",
                                   "abc", ":", "'", "\"", ",", "/", "?", "<", ">", LV_SYMBOL_BACKSPACE, "\n",
                                   "ABC", " ", ".", "~", "\\", "|", LV_SYMBOL_OK, ""};
// low 4 bits = relative width (1..15), the rest are flags
#define K2 (lv_buttonmatrix_ctrl_t)2
#define KS (lv_buttonmatrix_ctrl_t)(LV_KEYBOARD_CTRL_BUTTON_FLAGS | 3)
#define KSP (lv_buttonmatrix_ctrl_t)10
#define KOK (lv_buttonmatrix_ctrl_t)(LV_KEYBOARD_CTRL_BUTTON_FLAGS | 4)
static const lv_buttonmatrix_ctrl_t KB_CTRL[] = {K2, K2, K2, K2, K2, K2, K2, K2, K2, K2,
                                                 K2, K2, K2, K2, K2, K2, K2, K2, K2, K2,
                                                 K2, K2, K2, K2, K2, K2, K2, K2, K2,
                                                 KS, K2, K2, K2, K2, K2, K2, K2, KS,
                                                 KS, KSP, K2, K2, K2, K2, KOK};
static const lv_buttonmatrix_ctrl_t KB_CTRL_SPECIAL[] = {K2, K2, K2, K2, K2, K2, K2, K2, K2, K2,
                                                         K2, K2, K2, K2, K2, K2, K2, K2, K2, K2,
                                                         K2, K2, K2, K2, K2, K2, K2, K2, K2,
                                                         KS, K2, K2, K2, K2, K2, K2, K2, K2, KS,
                                                         KS, KSP, K2, K2, K2, K2, KOK};

static void ta_focus(lv_obj_t *ta) {
  for (lv_obj_t *t : {ta_ssid, ta_pass})
    if (t && t != ta) lv_obj_remove_state(t, LV_STATE_FOCUSED);
  lv_obj_add_state(ta, LV_STATE_FOCUSED);
  if (kb) lv_keyboard_set_textarea(kb, ta);
}
static void ta_cb(lv_event_t *e) {
  K.idle_at = millis();
  ta_focus((lv_obj_t *)lv_event_get_target(e));
}
static void kb_cb(lv_event_t *e) {
  K.idle_at = millis();
  if (lv_event_get_code(e) != LV_EVENT_READY) return;
  if (K.screen == Scr::Server) server_save();
  else wifi_try();
}
static void make_keyboard(lv_obj_t *col);
static lv_obj_t *field(lv_obj_t *p, const char *placeholder, uint32_t max_len) {
  lv_obj_t *ta = lv_textarea_create(p);
  lv_textarea_set_one_line(ta, true);
  lv_textarea_set_max_length(ta, max_len);
  lv_textarea_set_placeholder_text(ta, placeholder);
  lv_obj_set_height(ta, LV_PCT(100));
  lv_obj_set_width(ta, 10);
  lv_obj_set_flex_grow(ta, 1);
  lv_obj_set_style_text_font(ta, B28, 0);
  lv_obj_set_style_text_color(ta, lv_color_hex(C_INK), 0);
  lv_obj_set_style_radius(ta, 14, 0);
  lv_obj_set_style_bg_color(ta, lv_color_hex(C_PANEL), 0);
  lv_obj_set_style_border_width(ta, 2, 0);
  lv_obj_set_style_border_color(ta, lv_color_hex(C_LINE), 0);
  lv_obj_set_style_border_color(ta, lv_color_hex(C_PRI), LV_STATE_FOCUSED);
  lv_obj_set_style_pad_hor(ta, 16, 0);
  lv_obj_set_style_pad_ver(ta, 18, 0);
  lv_obj_add_event_cb(ta, ta_cb, LV_EVENT_CLICKED, nullptr);
  return ta;
}

static void v_wifi_pass() {
  lv_obj_t *col = box(body, LV_FLEX_FLOW_COLUMN, 12);
  lv_obj_set_size(col, LV_PCT(100), LV_PCT(100));
  lv_obj_t *t = box(col, LV_FLEX_FLOW_ROW, 14);
  lv_obj_set_flex_align(t, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
  text(t, K.hidden ? String("เพิ่มเครือข่ายที่ซ่อนอยู่") : "รหัสผ่าน WiFi · " + K.sel.ssid, B28, C_INK, false);
  wifi_msg = text(t, "", R18, C_MUTED, false);

  lv_obj_t *row = box(col, LV_FLEX_FLOW_ROW, 12);
  lv_obj_set_height(row, 78);
  ta_ssid = K.hidden ? field(row, "ชื่อ WiFi (SSID)", 32) : nullptr;
  ta_pass = field(row, K.hidden ? "รหัสผ่าน (เว้นว่างถ้าไม่มี)" : "รหัสผ่าน WiFi", 63);
  lv_textarea_set_password_mode(ta_pass, !K.show_pass);
  lv_textarea_set_password_bullet(ta_pass, LV_SYMBOL_BULLET);
  lv_obj_t *eye = small_button(row, "", C_NEUTRAL, A_WIFI_EYE, true);
  lv_obj_set_size(eye, 64, 78);
  eye_lbl = lv_obj_get_child(eye, 0);
  lv_obj_set_style_text_font(eye_lbl, &lv_font_montserrat_24, 0);
  lv_label_set_text(eye_lbl, K.show_pass ? LV_SYMBOL_EYE_CLOSE : LV_SYMBOL_EYE_OPEN);
  lv_obj_t *b = button(row, "ยกเลิก", C_PANEL, C_INK, A_WIFI_BACK, 78);
  lv_obj_set_width(b, 130);
  b = button(row, "เชื่อมต่อ", C_PRI, 0xFFFFFF, A_WIFI_CONNECT, 78);
  lv_obj_set_width(b, 170);

  make_keyboard(col);
  ta_focus(K.hidden ? ta_ssid : ta_pass);
  if (K.wifi_err.length()) wifi_msg_set(K.wifi_err, true);
  else wifi_msg_set(K.hidden ? "พิมพ์ชื่อให้ตรงทุกตัว ตัวเล็ก–ใหญ่มีผล" : "อย่างน้อย 8 ตัว · ตัวเล็ก–ใหญ่มีผล", false);
}

static void make_keyboard(lv_obj_t *col) {
  kb = lv_keyboard_create(col);
  lv_obj_set_width(kb, LV_PCT(100));
  lv_obj_set_flex_grow(kb, 1);
  lv_keyboard_set_map(kb, LV_KEYBOARD_MODE_TEXT_LOWER, KB_LOWER, KB_CTRL);
  lv_keyboard_set_map(kb, LV_KEYBOARD_MODE_TEXT_UPPER, KB_UPPER, KB_CTRL);
  lv_keyboard_set_map(kb, LV_KEYBOARD_MODE_SPECIAL, KB_SPECIAL, KB_CTRL_SPECIAL);
  lv_keyboard_set_mode(kb, LV_KEYBOARD_MODE_TEXT_LOWER);
  lv_obj_set_style_bg_color(kb, lv_color_hex(0xE7E5DF), 0);
  lv_obj_set_style_bg_opa(kb, LV_OPA_COVER, 0);
  lv_obj_set_style_border_width(kb, 0, 0);
  lv_obj_set_style_radius(kb, 16, 0);
  lv_obj_set_style_pad_all(kb, 10, 0);
  lv_obj_set_style_pad_gap(kb, 8, 0);
  lv_obj_set_style_text_font(kb, B28, LV_PART_ITEMS);
  lv_obj_set_style_radius(kb, 10, LV_PART_ITEMS);
  lv_obj_set_style_shadow_width(kb, 0, LV_PART_ITEMS);
  lv_obj_set_style_border_width(kb, 0, LV_PART_ITEMS);
  lv_obj_set_style_bg_color(kb, lv_color_hex(C_PANEL), LV_PART_ITEMS);
  lv_obj_set_style_text_color(kb, lv_color_hex(C_INK), LV_PART_ITEMS);
  lv_obj_set_style_bg_color(kb, lv_color_hex(0xD3D0C8), LV_PART_ITEMS | LV_STATE_CHECKED);
  lv_obj_set_style_text_color(kb, lv_color_hex(C_INK), LV_PART_ITEMS | LV_STATE_CHECKED);
  lv_obj_set_style_bg_color(kb, lv_color_hex(C_NEUTRAL_BG), LV_PART_ITEMS | LV_STATE_PRESSED);
  lv_obj_add_event_cb(kb, kb_cb, LV_EVENT_READY, nullptr);
  lv_obj_add_event_cb(kb, kb_cb, LV_EVENT_VALUE_CHANGED, nullptr);
}

// ---------- server address (laptop hotspot for the demo, or the real server) ----------
// preset "โน้ตบุ๊ก (hotspot)": with Windows Mobile Hotspot on, the laptop is always 192.168.137.1
static String short_base(const String &b) {
  String s = b;
  s.replace("https://", "");
  s.replace("http://", "");
  return s;
}
static void server_save() {
  if (!ta_ssid) return;
  String b = lv_textarea_get_text(ta_ssid);
  b.trim();
  while (b.endsWith("/")) b.remove(b.length() - 1);
  if (!b.startsWith("http://") && !b.startsWith("https://")) return wifi_msg_set("ต้องขึ้นต้นด้วย http:// หรือ https://", true);
  if (b.length() < 12) return wifi_msg_set("ที่อยู่สั้นเกินไป", true);
  api_set_base(b);
  go(Scr::WifiList);
}
static void v_server() {
  lv_obj_t *col = box(body, LV_FLEX_FLOW_COLUMN, 12);
  lv_obj_set_size(col, LV_PCT(100), LV_PCT(100));
  lv_obj_t *t = box(col, LV_FLEX_FLOW_ROW, 14);
  lv_obj_set_flex_align(t, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
  text(t, "ที่อยู่เซิร์ฟเวอร์ KitCheck", B28, C_INK, false);
  wifi_msg = text(t, "", R18, C_MUTED, false);

  lv_obj_t *row = box(col, LV_FLEX_FLOW_ROW, 12);
  lv_obj_set_height(row, 72);
  ta_ssid = field(row, "http://…", 96);
  lv_textarea_set_text(ta_ssid, api_base().c_str());
  lv_obj_t *b = button(row, "ยกเลิก", C_PANEL, C_INK, A_WIFI_BACK, 72);
  lv_obj_set_width(b, 130);
  b = button(row, "บันทึก", C_PRI, 0xFFFFFF, A_SERVER_SAVE, 72);
  lv_obj_set_width(b, 150);

  lv_obj_t *pr = box(col, LV_FLEX_FLOW_ROW, 12);
  lv_obj_set_flex_align(pr, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
  text(pr, "เลือกเร็ว:", R18, C_MUTED, false);
  small_button(pr, "โน้ตบุ๊ก (hotspot) · 192.168.137.1", C_INK, A_PRESET_LAPTOP, true);
  small_button(pr, "เซิร์ฟเวอร์จริง · " + short_base(KC_BASE), C_INK, A_PRESET_REAL, true);

  make_keyboard(col);
  ta_focus(ta_ssid);
  wifi_msg_set("เปลี่ยนแล้วตู้จะล็อกอินที่ใหม่เอง", false);
}

static void v_wifi_ok() {
  lv_obj_t *col = box(body, LV_FLEX_FLOW_COLUMN, 12);
  lv_obj_set_size(col, LV_PCT(100), LV_PCT(100));
  lv_obj_set_flex_align(col, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
  big_mark(col, C_OK, LV_SYMBOL_OK);
  text(col, "เชื่อมต่อ WiFi แล้ว", B40, C_OK, false);
  String ssid = K.W.connected ? K.W.ssid : K.sel.ssid;
  String line = ssid;
  if (K.W.connected) line += " · สัญญาณ" + quality(K.W.rssi) + " · IP " + K.W.ip;
  text(col, line, R22, C_INK, false);
  if (K.server_ok) text(col, "ติดต่อเซิร์ฟเวอร์ KitCheck ได้ พร้อมใช้งาน", B22, C_OK, false);
  else text(col, "ต่อ WiFi ได้ แต่ยังติดต่อเซิร์ฟเวอร์ KitCheck ไม่ได้ (เช็กว่าเครื่องเซิร์ฟเวอร์เปิดอยู่)", B22, C_WA, false);
  text(col, "บันทึกไว้ในเครื่องแล้ว ปิด–เปิดตู้ครั้งหน้าจะต่อ WiFi นี้เอง", R18, C_MUTED, false);
  lv_obj_t *b = button(col, "กลับหน้าแรก", C_PANEL, C_INK, A_HOME, 68);
  lv_obj_set_width(b, 320);
}

static void v_borrow() {
  bool blocked = K.has_scan && K.scan.status == "blocked";
  lv_obj_t *l, *r;
  columns(&l, &r, true);
  camera_view(l);
  lv_obj_t *w = panel(l);
  lv_obj_set_flex_grow(w, 1);
  lv_obj_t *top = box(w, LV_FLEX_FLOW_ROW, 10);
  lv_obj_set_flex_align(top, LV_FLEX_ALIGN_SPACE_BETWEEN, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
  text(top, "น้ำหนักบนถาด", R18, C_MUTED, false);
  live_pill = text(top, "", R18, C_NEUTRAL, false);
  lv_obj_set_style_bg_opa(live_pill, LV_OPA_COVER, 0);
  lv_obj_set_style_radius(live_pill, 999, 0);
  lv_obj_set_style_pad_hor(live_pill, 12, 0);
  lv_obj_set_style_pad_ver(live_pill, 4, 0);
  live_weight = text(w, "", B40);

  lv_obj_t *p = scroll_panel(r);
  if (blocked) {
    lv_obj_t *b = banner(p, C_BD_BG);
    text(b, "ยืมไม่ได้ ต้องแก้ก่อน", B28, C_BD);
    for (auto &i : K.scan.issues) text(b, i, R22, C_BD);
    text(b, "หยิบชิ้นที่มีปัญหาออก หรือหันป้าย QR ขึ้น ระบบจะตรวจใหม่เอง", R18, C_INK);
    for (auto &d : K.scan.dets) det_row(p, d, true);
  } else {
    text(p, "วางเครื่องมือที่จะยืมบนถาด", B28);
    text(p, "วางเสร็จแล้วปล่อยมือ ระบบตรวจเองเมื่อถาดนิ่ง", R22, C_MUTED);
    if (K.member.loans.size()) {
      String s = "ยืมอยู่แล้ว: ";
      for (size_t i = 0; i < K.member.loans.size(); i++) s += (i ? " · " : "") + K.member.loans[i].type_name + " " + K.member.loans[i].qr;
      text(p, s, R18, C_INK);
    }
  }
  err_label(p);
  button(r, blocked ? "ตรวจอีกครั้ง" : "ตรวจเลย", C_PRI, 0xFFFFFF, A_SCAN);
  button(r, "ยกเลิก", C_PANEL, C_INK, A_CANCEL, 64);
}

static void v_confirm() {
  const Scan &sc = K.scan;
  lv_obj_t *l, *r;
  columns(&l, &r);
  lv_obj_t *p = scroll_panel(l);
  text(p, "จะยืม " + String(sc.dets.size()) + " ชิ้น", B40);
  text(p, "คืนภายใน " + fmt_date(sc.due) + " (" + String(S.loan_days) + " วัน) · คืนที่ตู้นี้ได้เลย", R22, C_MUTED);
  for (auto &d : sc.dets) det_row(p, d, true);
  String wl = weight_line(sc, true);
  if (wl.length()) text(p, wl, R18, C_MUTED);

  lv_obj_t *q = panel(r);
  lv_obj_set_flex_grow(q, 1);
  lv_obj_set_flex_align(q, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
  count_arc = lv_arc_create(q);
  lv_obj_set_size(count_arc, 190, 190);
  lv_arc_set_bg_angles(count_arc, 0, 360);
  lv_arc_set_rotation(count_arc, 270);
  lv_arc_set_range(count_arc, 0, 1000);
  lv_arc_set_value(count_arc, 1000);
  lv_obj_remove_style(count_arc, nullptr, LV_PART_KNOB);
  lv_obj_remove_flag(count_arc, LV_OBJ_FLAG_CLICKABLE);
  lv_obj_set_style_arc_width(count_arc, 14, LV_PART_MAIN);
  lv_obj_set_style_arc_color(count_arc, lv_color_hex(C_NEUTRAL_BG), LV_PART_MAIN);
  lv_obj_set_style_arc_width(count_arc, 14, LV_PART_INDICATOR);
  lv_obj_set_style_arc_color(count_arc, lv_color_hex(C_PRI), LV_PART_INDICATOR);
  count_lbl = text(count_arc, "5", B64, C_INK, false);
  lv_obj_center(count_lbl);
  lv_obj_t *t = text(q, "บันทึกการยืมอัตโนมัติ", B22);
  lv_obj_set_style_text_align(t, LV_TEXT_ALIGN_CENTER, 0);
  t = text(q, "ถ้าไม่ใช่ของที่ต้องการ กดยกเลิก", R18, C_MUTED);
  lv_obj_set_style_text_align(t, LV_TEXT_ALIGN_CENTER, 0);
  lv_obj_t *row = box(r, LV_FLEX_FLOW_ROW, 12);
  lv_obj_t *b = button(row, "ยกเลิก", C_PANEL, C_INK, A_CANCEL);
  lv_obj_set_width(b, 10);
  lv_obj_set_flex_grow(b, 1);
  b = button(row, "ยืมเลย", C_PRI, 0xFFFFFF, A_CONFIRM);
  lv_obj_set_width(b, 10);
  lv_obj_set_flex_grow(b, 1);
}

static lv_obj_t *big_mark(lv_obj_t *p, uint32_t bg, const char *sym) {
  lv_obj_t *c = lv_obj_create(p);
  lv_obj_remove_style_all(c);
  lv_obj_set_size(c, 112, 112);
  lv_obj_set_style_radius(c, LV_RADIUS_CIRCLE, 0);
  lv_obj_set_style_bg_color(c, lv_color_hex(bg), 0);
  lv_obj_set_style_bg_opa(c, LV_OPA_COVER, 0);
  lv_obj_t *l = lv_label_create(c);
  lv_label_set_text(l, sym);
  lv_obj_set_style_text_font(l, &lv_font_montserrat_48, 0);
  lv_obj_set_style_text_color(l, lv_color_hex(0xFFFFFF), 0);
  lv_obj_center(l);
  return c;
}

static void v_done() {
  const Scan &sc = K.scan;
  lv_obj_t *col = box(body, LV_FLEX_FLOW_COLUMN, 14);
  lv_obj_set_size(col, LV_PCT(100), LV_PCT(100));
  lv_obj_set_flex_align(col, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
  big_mark(col, C_OK, LV_SYMBOL_OK);
  lv_obj_t *t = text(col, "บันทึกการยืมแล้ว", B40, C_OK, false);
  String items;
  for (size_t i = 0; i < sc.dets.size(); i++) items += (i ? " · " : "") + sc.dets[i].type_name;
  t = text(col, items + " · คืนภายใน " + fmt_date(sc.due), R22);
  lv_obj_set_style_text_align(t, LV_TEXT_ALIGN_CENTER, 0);
  text(col, "ยกของออกจากถาดได้เลย", B28, C_INK, false);
  text(col, "รอบ " + sc.id + " · กลับหน้าแรกเองเมื่อถาดว่าง", R18, C_MUTED, false);
  lv_obj_t *b = button(col, "กลับหน้าแรก", C_PANEL, C_INK, A_HOME, 68);
  lv_obj_set_width(b, 320);
}

static void v_busy(const char *msg) {
  lv_obj_t *col = box(body, LV_FLEX_FLOW_COLUMN, 24);
  lv_obj_set_size(col, LV_PCT(100), LV_PCT(100));
  lv_obj_set_flex_align(col, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
  lv_obj_t *sp = lv_spinner_create(col);
  lv_obj_set_size(sp, 96, 96);
  lv_obj_set_style_arc_color(sp, lv_color_hex(C_PRI), LV_PART_INDICATOR);
  text(col, msg, B28, C_INK, false);
}

// return rounds: split detections the same way the web kiosk does
struct Split {
  std::vector<const Det *> ok, bad, notout;
};
static Split split(const Scan &sc) {
  Split s;
  for (auto &d : sc.dets) {
    if (!d.issue.length() && d.has_loan) s.ok.push_back(&d);
    else if (d.issue == "notout") s.notout.push_back(&d);
    else if (d.issue.length()) s.bad.push_back(&d);
  }
  return s;
}
static void remaining_banner(lv_obj_t *p, const String &title, const std::vector<Loan> &loans) {
  lv_obj_t *b = banner(p, C_WA_BG);
  text(b, title, B22, C_WA);
  for (auto &l : loans) text(b, l.type_name + " " + l.qr + " · คืน " + fmt_date(l.due), R18, C_INK);
}
static String list_types(const std::vector<const Det *> &v) {
  String s;
  for (size_t i = 0; i < v.size(); i++) s += (i ? " · " : "") + v[i]->type_name + (v[i]->qr.length() ? " " + v[i]->qr : String());
  return s;
}

static void v_retconfirm() {
  const Scan &sc = K.scan;
  Split s = split(sc);
  lv_obj_t *l, *r;
  columns(&l, &r, true);
  camera_view(l);
  lv_obj_t *h = panel(l);
  lv_obj_set_flex_grow(h, 1);
  text(h, s.ok.size() ? "จะคืน " + String(s.ok.size()) + " ชิ้น" : String("ส่งให้เจ้าหน้าที่ตรวจ"), B40);
  String wl = weight_line(sc, false);
  if (wl.length()) text(h, wl, R18, C_MUTED);

  lv_obj_t *p = scroll_panel(r);
  for (auto d : s.ok) det_row(p, *d, false, "ของ " + d->owner);
  if (s.bad.size()) {
    lv_obj_t *b = banner(p, C_WA_BG);
    text(b, "อีก " + String(s.bad.size()) + " ชิ้นระบบอ่านไม่ได้ · จะส่งให้เจ้าหน้าที่ตรวจ", B22, C_WA);
    for (auto d : s.bad) text(b, d->type_name + " · " + d->issue_th, R18, C_INK);
  }
  if (s.notout.size()) text(p, "ไม่ได้ถูกยืมอยู่ (ไม่นับ): " + list_types(s.notout), R18, C_MUTED);
  bool any_left = false;
  for (auto &rm : sc.remaining) {
    std::vector<Loan> left;
    for (auto &ln : rm.loans) {
      bool back = false;
      for (auto d : s.ok) back |= d->qr == ln.qr;
      if (!back) left.push_back(ln);
    }
    if (left.size()) {
      any_left = true;
      remaining_banner(p, "คืนไม่ครบ · ยังค้างในชื่อ " + rm.member, left);
    }
  }
  if (s.ok.size() && !any_left) {
    lv_obj_t *b = banner(p, C_OK_BG);
    text(b, "คืนครบทุกชิ้นที่ยืมไป", B22, C_OK);
  }
  err_label(p);
  text(r, "แน่ใจว่าจะคืนตามนี้?", B22);
  button(r, "ยืนยันคืน", C_OK, 0xFFFFFF, A_CONFIRM);
  button(r, "ยกเลิก", C_PANEL, C_INK, A_CANCEL, 64);
}

static void v_returned() {
  const Scan &sc = K.scan;
  Split s = split(sc);
  lv_obj_t *l, *r;
  columns(&l, &r, true);
  camera_view(l);
  lv_obj_t *h = panel(l);
  lv_obj_set_flex_grow(h, 1);
  lv_obj_set_flex_flow(h, LV_FLEX_FLOW_ROW);
  lv_obj_set_flex_align(h, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
  lv_obj_set_style_pad_column(h, 18, 0);
  lv_obj_t *mk = big_mark(h, s.ok.size() ? C_OK : C_WA, s.ok.size() ? LV_SYMBOL_OK : LV_SYMBOL_WARNING);
  lv_obj_set_size(mk, 84, 84);
  lv_obj_t *hc = box(h, LV_FLEX_FLOW_COLUMN, 2);
  lv_obj_set_width(hc, 10);
  lv_obj_set_flex_grow(hc, 1);
  text(hc, s.ok.size() ? "คืนแล้ว " + String(s.ok.size()) + " ชิ้น" : String("ยกของออกจากถาด"), B40, s.ok.size() ? C_OK : C_INK);
  text(hc, s.ok.size() ? "ยกของไปเก็บที่ชั้นตามป้ายได้เลย" : "กลับหน้าแรกเองเมื่อถาดว่าง", R18, C_MUTED);

  lv_obj_t *p = scroll_panel(r);
  if (s.ok.size()) {
    lv_obj_t *b = banner(p, C_OK_BG);
    text(b, "คืนแล้ว " + String(s.ok.size()) + " ชิ้น " LV_SYMBOL_OK, B28, C_OK);
    String who;
    for (auto d : s.ok)
      if (who.indexOf(d->owner) < 0) who += (who.length() ? ", " : "") + d->owner;
    text(b, "ของ " + who, R18, C_INK);
    for (auto d : s.ok) text(b, d->type_name + "  " + d->qr, R18, C_INK);
  }
  if (s.bad.size() || sc.issues.size()) {
    lv_obj_t *b = banner(p, C_WA_BG);
    text(b, "ส่งให้เจ้าหน้าที่ตรวจ" + (s.bad.size() ? " " + String(s.bad.size()) + " ชิ้น" : String()), B22, C_WA);
    for (auto d : s.bad) text(b, d->type_name + " · " + d->issue_th, R18, C_INK);
    for (auto &i : sc.issues) text(b, i, R18, C_INK);
    text(b, "วางชิ้นนั้นไว้ที่ชั้น \"รอตรวจ\" ระบบจะแจ้งเจ้าหน้าที่", R18, C_INK);
  }
  if (s.notout.size() && !s.ok.size() && !s.bad.size()) {
    lv_obj_t *b = banner(p, C_BD_BG);
    text(b, "ของเหล่านี้ไม่ได้ถูกยืมอยู่", B22, C_BD);
    text(b, "ถ้าจะยืม กดยืมแล้วใส่รหัสก่อน แล้ววางของใหม่", R18, C_INK);
  } else if (s.notout.size()) {
    text(p, "ไม่ได้ถูกยืมอยู่: " + list_types(s.notout), R18, C_MUTED);
  }
  if (!sc.dets.size()) {
    lv_obj_t *b = banner(p, C_BD_BG);
    text(b, "ไม่พบเครื่องมือบนถาด", B22, C_BD);
    text(b, "หันป้าย QR ขึ้น แล้วลองใหม่", R18, C_INK);
  }
  for (auto &rm : sc.remaining) remaining_banner(p, "ยังค้างอยู่ในชื่อ " + rm.member, rm.loans);
  String wl = weight_line(sc, false);
  if (wl.length()) text(p, wl, R18, C_MUTED);
  button(r,"เสร็จ · กลับหน้าแรก", C_PANEL, C_INK, A_HOME);
}

// ---------- header + live values ----------
static void build_header_left() {
  lv_obj_clean(hdr_left);
  bool member_hdr = K.has_member && (K.screen == Scr::Borrow || K.screen == Scr::Confirm || K.screen == Scr::Done);
  if (member_hdr) {
    lv_obj_t *col = box(hdr_left, LV_FLEX_FLOW_COLUMN, 0);
    lv_obj_set_width(col, LV_SIZE_CONTENT);
    text(col, (K.screen == Scr::Borrow ? "สวัสดี " : "") + K.member.name, B22, 0xFFFFFF, false);
    text(col, "รหัส " + K.member.code + " · ยืมอยู่ " + String(K.member.loans.size()) + " ชิ้น", R18, C_HDR_MUTED, false);
    return;
  }
  lv_obj_t *logo = lv_obj_create(hdr_left);
  lv_obj_remove_style_all(logo);
  lv_obj_set_size(logo, 36, 36);
  lv_obj_set_style_radius(logo, 9, 0);
  lv_obj_set_style_bg_color(logo, lv_color_hex(C_PRI), 0);
  lv_obj_set_style_bg_opa(logo, LV_OPA_COVER, 0);
  lv_obj_t *ok = lv_label_create(logo);
  lv_label_set_text(ok, LV_SYMBOL_OK);
  lv_obj_set_style_text_font(ok, &lv_font_montserrat_24, 0);
  lv_obj_set_style_text_color(ok, lv_color_hex(0xFFFFFF), 0);
  lv_obj_center(ok);
  text(hdr_left, "KitCheck", B22, 0xFFFFFF, false);
  text(hdr_left, "· ตู้ยืม–คืนเครื่องมือ", R18, C_HDR_MUTED, false);
}

static void set_pill(const char *s, uint32_t bg, uint32_t fg) {
  lv_label_set_text(live_pill, s);
  lv_obj_set_style_bg_color(live_pill, lv_color_hex(bg), 0);
  lv_obj_set_style_text_color(live_pill, lv_color_hex(fg), 0);
}

static void update_live() {
  String prob = api_problem();
  int kind = prob.length() ? 0 : !S.camera_ok ? 1 : 2;
  String st = kind == 0 ? String(LV_SYMBOL_BULLET " ") + prob
            : kind == 1 ? String(LV_SYMBOL_BULLET " กล้องไม่เชื่อมต่อ")
                        : String(LV_SYMBOL_BULLET " พร้อมใช้งาน");
  set_text(lbl_status, st.c_str());
  if (kind != status_kind) {
    status_kind = kind;
    lv_obj_set_style_text_color(lbl_status, lv_color_hex(kind == 2 ? 0x7BD88F : 0xF28B82), 0);
  }
  if (S.sim != !lv_obj_has_flag(lbl_sim, LV_OBJ_FLAG_HIDDEN)) {
    if (S.sim) lv_obj_remove_flag(lbl_sim, LV_OBJ_FLAG_HIDDEN);
    else lv_obj_add_flag(lbl_sim, LV_OBJ_FLAG_HIDDEN);
  }
  int ck = K.W.connected ? 1 : 0;
  set_text(wifi_chip_lbl, (String(LV_SYMBOL_WIFI " ") + (K.W.connected ? K.W.ssid : String("ตั้งค่า WiFi"))).c_str());
  if (ck != chip_kind) {
    chip_kind = ck;
    lv_obj_set_style_text_color(wifi_chip_lbl, lv_color_hex(ck ? 0xFFFFFF : 0xF28B82), 0);
  }
  String g = S.fresh ? fmt_g(S.grams) : String("—");
  set_text(lbl_tray, (String("ถาด ") + g).c_str());
  set_text(lbl_clock, clock_text().c_str());
  if (live_weight) set_text(live_weight, g.c_str());

  if (live_pill) {
    int pk = K.busy ? 0 : !S.fresh ? 1 : !on_tray() ? 2 : !S.stable ? 3 : 4;
    if (pk != pill_kind) {
      pill_kind = pk;
      switch (pk) {
        case 0: set_pill(LV_SYMBOL_BULLET " กำลังตรวจ…", C_INFO_BG, C_INFO); break;
        case 1: set_pill("ไม่มีสัญญาณถาดชั่ง · กด ตรวจเลย", C_NEUTRAL_BG, C_NEUTRAL); break;
        case 2: set_pill("รอวางเครื่องมือ", C_NEUTRAL_BG, C_NEUTRAL); break;
        case 3: set_pill(LV_SYMBOL_BULLET " รอให้ถาดนิ่ง…", C_WA_BG, C_WA); break;
        default: set_pill(LV_SYMBOL_BULLET " ถาดนิ่ง", C_OK_BG, C_OK); break;
      }
    }
  }
  if (cam_msg && !cam_frame) {
    String cp = cam_problem();
    set_text(cam_msg, cp.length() ? cp.c_str() : "กำลังเปิดกล้อง…");
  }
}

static void render() {
  lv_obj_clean(body);
  live_weight = live_pill = count_arc = count_lbl = code_lbl = nullptr;
  ta_ssid = ta_pass = kb = wifi_msg = eye_lbl = nullptr;
  cam_img = cam_msg = cam_layer = nullptr;
  pill_kind = -1;
  bool want_cam = K.screen == Scr::Borrow || K.screen == Scr::RetConfirm || K.screen == Scr::Returned;
  static bool cam_on = false;
  if (want_cam && !cam_on) cam_frame = nullptr;  // don't flash an old picture
  cam_on = want_cam;
  cam_enable(want_cam);
  build_header_left();
  switch (K.screen) {
    case Scr::Idle: v_idle(); break;
    case Scr::Keypad: v_keypad(); break;
    case Scr::Borrow: v_borrow(); break;
    case Scr::Confirm: v_confirm(); break;
    case Scr::Done: v_done(); break;
    case Scr::Returning: v_busy("กำลังตรวจของที่คืน…"); break;
    case Scr::RetConfirm: v_retconfirm(); break;
    case Scr::Returned: v_returned(); break;
    case Scr::Offline: v_offline(); break;
    case Scr::Pin: v_pin(); break;
    case Scr::WifiList: v_wifi_list(); break;
    case Scr::WifiPass: v_wifi_pass(); break;
    case Scr::WifiOk: v_wifi_ok(); break;
    case Scr::Server: v_server(); break;
  }
  bool show_overlay = K.busy && K.screen != Scr::Returning;
  if (show_overlay) {
    bool wifi = K.screen == Scr::WifiList || K.screen == Scr::WifiPass;
    lv_label_set_text(overlay_lbl, wifi ? ("กำลังเชื่อมต่อ " + K.sel.ssid + "…").c_str()
                                   : K.screen == Scr::Keypad ? "กำลังค้นหาสมาชิก…"
                                   : K.screen == Scr::Borrow ? "กำลังตรวจของบนถาด…"
                                                             : "กำลังบันทึก…");
    lv_obj_remove_flag(overlay, LV_OBJ_FLAG_HIDDEN);
  } else {
    lv_obj_add_flag(overlay, LV_OBJ_FLAG_HIDDEN);
  }
  update_live();
}

// ---------- the kiosk loop (same rules as tick() in kiosk.html) ----------
static void tick_cb(lv_timer_t *) {
  Result r;
  if (api_take_result(r)) on_result(r);
  S = api_state();
  std::vector<CamBox> bx;
  if (const lv_image_dsc_t *f = cam_take_frame(bx)) {
    cam_frame = f;
    cam_boxes = bx;
    cam_show_frame();
  }
  uint32_t idle = millis() - K.idle_at;

  // ---- WiFi ----
  K.W = wifi_info();
  std::vector<WifiNet> nets;
  if (wifi_take_scan(nets)) {
    K.nets = nets;
    K.scanning = false;
    if (K.screen == Scr::WifiList) render();
  }
  String werr;
  bool server_ok;
  int cr = wifi_take_connect(werr, server_ok);
  if (cr) {
    K.busy = false;
    lv_obj_add_flag(overlay, LV_OBJ_FLAG_HIDDEN);
    if (cr > 0) {
      K.server_ok = server_ok;
      go(Scr::WifiOk);
    } else if (K.screen == Scr::WifiPass) {
      K.wifi_err = werr;
      wifi_msg_set("ต่อไม่สำเร็จ: " + werr, true);  // keep what was typed
    } else {
      K.wifi_err = "ต่อ " + K.sel.ssid + " ไม่สำเร็จ: " + werr;
      render();
    }
  }
  static bool was_conn = false;
  if (K.W.connected != was_conn) {
    was_conn = K.W.connected;
    if (K.screen == Scr::WifiList && !K.busy) render();
  }
  bool want_offline = K.W.ready && !K.W.connected && (!K.W.has_saved || K.W.down_ms > 20000);
  if (K.screen == Scr::Idle && !K.busy && want_offline) go(Scr::Offline);
  else if (K.screen == Scr::Offline && K.W.connected) reset();
  if (!K.busy) {
    if (K.screen == Scr::Pin && idle > 60000) reset();
    if ((K.screen == Scr::WifiList || K.screen == Scr::WifiPass || K.screen == Scr::Server) && idle > 180000) reset();
    if (K.screen == Scr::WifiOk && idle > 15000) reset();
  }

  if (S.fresh && !isnan(S.grams) && S.grams <= S.empty_g) K.armed = true;
  if (!K.busy && S.online) {
    if (K.screen == Scr::Idle && K.armed && S.stable && on_tray()) start_scan("return");
    else if (K.screen == Scr::Borrow && S.stable && on_tray() &&
             (isnan(K.last_scan_g) || fabsf(S.grams - K.last_scan_g) > 20))
      start_scan("borrow");
    else if ((K.screen == Scr::Done || K.screen == Scr::Returned) && S.fresh && !on_tray() && idle > 2500)
      reset();
  }
  if (!K.busy) {
    if (K.screen == Scr::Borrow && idle > 90000) reset();
    if (K.screen == Scr::Keypad && idle > 60000) reset();
    if (K.screen == Scr::RetConfirm && idle > 90000) cancel_round();  // nobody confirmed: nothing recorded
    if ((K.screen == Scr::Done || K.screen == Scr::Returned) && idle > 15000) reset();
  }
  if (K.screen == Scr::Confirm && !K.busy && count_arc) {
    int32_t left = 5000 - (int32_t)(millis() - K.count_at);
    if (left <= 0) {
      confirm_scan();
    } else {
      lv_arc_set_value(count_arc, left / 5);
      char b[4];
      snprintf(b, sizeof b, "%d", (int)((left + 999) / 1000));
      lv_label_set_text(count_lbl, b);
    }
  }
  update_live();
}

static lv_font_t *ttf(const uint8_t *data, size_t len, int px, const lv_font_t *fallback) {
  lv_font_t *f = lv_tiny_ttf_create_data(data, len, px);
  if (f) f->fallback = fallback;  // LV_SYMBOL_* icons come from Montserrat
  return f;
}

void ui_begin() {
  R18 = ttf(font_thai_regular, font_thai_regular_len, 18, &lv_font_montserrat_14);
  R22 = ttf(font_thai_regular, font_thai_regular_len, 22, &lv_font_montserrat_24);
  B22 = ttf(font_thai_bold, font_thai_bold_len, 22, &lv_font_montserrat_24);
  B28 = ttf(font_thai_bold, font_thai_bold_len, 28, &lv_font_montserrat_24);
  B40 = ttf(font_thai_bold, font_thai_bold_len, 40, &lv_font_montserrat_48);
  B64 = ttf(font_thai_bold, font_thai_bold_len, 64, &lv_font_montserrat_48);

  lv_display_t *disp = lv_display_get_default();
  lv_display_set_theme(disp, lv_theme_default_init(disp, lv_color_hex(C_PRI), lv_color_hex(C_OK), false, R22));

  lv_obj_t *scr = lv_screen_active();
  lv_obj_set_style_bg_color(scr, lv_color_hex(C_BG), 0);
  lv_obj_set_style_text_font(scr, R22, 0);
  lv_obj_remove_flag(scr, LV_OBJ_FLAG_SCROLLABLE);

  lv_obj_t *hdr = lv_obj_create(scr);
  lv_obj_remove_style_all(hdr);
  lv_obj_set_size(hdr, SCREEN_W, 72);
  lv_obj_set_style_bg_color(hdr, lv_color_hex(C_HDR), 0);
  lv_obj_set_style_bg_opa(hdr, LV_OPA_COVER, 0);
  lv_obj_set_style_pad_hor(hdr, 24, 0);
  lv_obj_set_flex_flow(hdr, LV_FLEX_FLOW_ROW);
  lv_obj_set_flex_align(hdr, LV_FLEX_ALIGN_SPACE_BETWEEN, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
  lv_obj_remove_flag(hdr, LV_OBJ_FLAG_SCROLLABLE);
  hdr_left = box(hdr, LV_FLEX_FLOW_ROW, 12);
  lv_obj_set_width(hdr_left, LV_SIZE_CONTENT);
  lv_obj_set_flex_align(hdr_left, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
  lv_obj_t *right = box(hdr, LV_FLEX_FLOW_ROW, 22);
  lv_obj_set_width(right, LV_SIZE_CONTENT);
  lv_obj_set_flex_align(right, LV_FLEX_ALIGN_END, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
  lbl_status = text(right, "", R18, 0x7BD88F, false);
  // WiFi chip: shows the network, opens the WiFi settings (staff PIN first)
  lv_obj_t *chip = lv_button_create(right);
  lv_obj_set_height(chip, 44);
  lv_obj_set_style_pad_hor(chip, 14, 0);
  lv_obj_set_style_radius(chip, 999, 0);
  lv_obj_set_style_shadow_width(chip, 0, 0);
  lv_obj_set_style_bg_color(chip, lv_color_hex(0x2A2D32), 0);
  lv_obj_set_style_bg_color(chip, lv_color_hex(0x3A3D42), LV_STATE_PRESSED);
  lv_obj_set_style_border_width(chip, 1, 0);
  lv_obj_set_style_border_color(chip, lv_color_hex(0x3A3D42), 0);
  wifi_chip_lbl = lv_label_create(chip);
  lv_label_set_text(wifi_chip_lbl, "");
  lv_obj_set_style_text_font(wifi_chip_lbl, R18, 0);
  lv_obj_center(wifi_chip_lbl);
  lv_obj_add_event_cb(chip, act_cb, LV_EVENT_CLICKED, (void *)(intptr_t)A_WIFI);
  lbl_sim = text(right, "ผลตรวจ: จำลอง", R18, 0xF5C26B, false);
  lbl_tray = text(right, "", R18, 0xFFFFFF, false);
  lbl_clock = text(right, "", B22, 0xFFFFFF, false);

  body = lv_obj_create(scr);
  lv_obj_remove_style_all(body);
  lv_obj_set_pos(body, 0, 72);
  lv_obj_set_size(body, SCREEN_W, SCREEN_H - 72);
  lv_obj_set_style_pad_all(body, 20, 0);
  lv_obj_remove_flag(body, LV_OBJ_FLAG_SCROLLABLE);

  // busy overlay: blocks touches while a request is running
  overlay = lv_obj_create(lv_layer_top());
  lv_obj_remove_style_all(overlay);
  lv_obj_set_size(overlay, SCREEN_W, SCREEN_H);
  lv_obj_set_style_bg_color(overlay, lv_color_hex(0x000000), 0);
  lv_obj_set_style_bg_opa(overlay, LV_OPA_40, 0);
  lv_obj_add_flag(overlay, LV_OBJ_FLAG_CLICKABLE);
  lv_obj_t *card = panel(overlay, 32);
  lv_obj_set_width(card, 420);
  lv_obj_center(card);
  lv_obj_set_flex_align(card, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
  lv_obj_set_style_pad_row(card, 18, 0);
  lv_obj_t *sp = lv_spinner_create(card);
  lv_obj_set_size(sp, 72, 72);
  lv_obj_set_style_arc_color(sp, lv_color_hex(C_PRI), LV_PART_INDICATOR);
  overlay_lbl = text(card, "", B28, C_INK, false);
  lv_obj_add_flag(overlay, LV_OBJ_FLAG_HIDDEN);

  K.idle_at = millis();
  render();
  lv_timer_create(tick_cb, 100, nullptr);
}
