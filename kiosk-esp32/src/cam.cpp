#include "cam.h"

#include <ArduinoJson.h>
#include <HTTPClient.h>
#include <JPEGDEC.h>
#include <NetworkClientSecure.h>
#include <WiFi.h>
#include <esp_heap_caps.h>
#include <new>

#include "api.h"
#include "ca_roots.h"
#include "secrets.h"

static constexpr size_t JPG_MAX = 600 * 1024;

static SemaphoreHandle_t mu;
static volatile bool enabled = false;
static uint16_t *bufs[3];
static lv_image_dsc_t dscs[3];
static std::vector<CamBox> box_sets[3];
static int front = -1, ready = -1;
static String problem = "กำลังเปิดกล้อง…";

struct Lock {
  Lock() { xSemaphoreTake(mu, portMAX_DELAY); }
  ~Lock() { xSemaphoreGive(mu); }
};

void cam_enable(bool on) {
  if (on && !enabled) {  // drop a frame left over from the last time the camera was on screen
    Lock l;
    ready = -1;
    problem = "กำลังเปิดกล้อง…";
  }
  enabled = on;
}
String cam_problem() { Lock l; return problem; }
static void set_problem(const String &p) {
  Serial.printf("[cam] %s\n", p.c_str());
  Lock l;
  problem = p;
}

const lv_image_dsc_t *cam_take_frame(std::vector<CamBox> &boxes) {
  Lock l;
  if (ready < 0) return nullptr;
  front = ready;
  ready = -1;
  boxes = box_sets[front];
  return &dscs[front];
}

// ---------- HTTP body into a fixed buffer (handles chunked replies too) ----------
struct BufStream : public Stream {
  uint8_t *buf;
  size_t cap, len = 0;
  bool overflow = false;
  BufStream(uint8_t *b, size_t c) : buf(b), cap(c) {}
  size_t write(uint8_t c) override { return write(&c, 1); }
  size_t write(const uint8_t *b, size_t n) override {
    size_t k = min(n, cap - len);
    memcpy(buf + len, b, k);
    len += k;
    if (k < n) overflow = true;
    return n;
  }
  int available() override { return 0; }
  int read() override { return -1; }
  int peek() override { return -1; }
};

// ---------- JPEG -> centre-cropped RGB565 frame ----------
struct DecodeTarget {
  uint16_t *px;
  int ox, oy;  // crop offset in decoded pixels
};

static int draw_cb(JPEGDRAW *d) {
  auto *t = (DecodeTarget *)d->pUser;
  for (int row = 0; row < d->iHeight; row++) {
    int y = d->y + row - t->oy;
    if (y < 0 || y >= CAM_H) continue;
    int x0 = d->x - t->ox, sx = 0, w = d->iWidth;
    if (x0 < 0) { sx = -x0; w += x0; x0 = 0; }
    if (x0 + w > CAM_W) w = CAM_W - x0;
    if (w > 0) memcpy(t->px + y * CAM_W + x0, d->pPixels + row * d->iWidth + sx, w * 2);
  }
  return 1;
}

static JPEGDEC *jpg;

// Returns false when the data is not a JPEG we can decode. dw/dh = decoded size, ox/oy = crop offset.
static bool decode(const uint8_t *data, size_t len, uint16_t *out, int &dw, int &dh, int &ox, int &oy) {
  if (!jpg->openRAM((uint8_t *)data, (int)len, draw_cb)) return false;
  int w = jpg->getWidth(), h = jpg->getHeight();
  // largest reduction that still covers the view (1280x720 -> 1/2 = 640x360)
  static const int opts[] = {0, JPEG_SCALE_HALF, JPEG_SCALE_QUARTER, JPEG_SCALE_EIGHTH};
  int shift = 0;
  while (shift < 3 && (w >> (shift + 1)) >= CAM_W && (h >> (shift + 1)) >= CAM_H) shift++;
  dw = w >> shift;
  dh = h >> shift;
  ox = (dw - CAM_W) / 2;
  oy = (dh - CAM_H) / 2;
  if (dw < CAM_W || dh < CAM_H) memset(out, 0, CAM_W * CAM_H * 2);
  DecodeTarget t{out, ox, oy};
  jpg->setUserPointer(&t);
  jpg->setPixelType(RGB565_LITTLE_ENDIAN);
  int ok = jpg->decode(0, 0, opts[shift]);
  jpg->close();
  return ok == 1;
}

// ---------- publishing a decoded frame ----------
static uint8_t *jbuf;
static uint32_t frames = 0;

static int back_buffer() {
  Lock l;
  int b = 0;
  while (b == front || b == ready) b++;
  return b;
}

// JPEG in jbuf -> next frame for the UI. dets_json = CiRA boxes as 0..1 of the picture.
static bool publish(size_t len, const String &dets_json, uint32_t got_ms) {
  int back = back_buffer(), dw, dh, ox, oy;
  uint32_t t0 = millis();
  if (!decode(jbuf, len, bufs[back], dw, dh, ox, oy)) {
    set_problem("ภาพจากกล้องเสีย");
    return false;
  }
  std::vector<CamBox> boxes;
  JsonDocument doc;
  if (dets_json.length() && !deserializeJson(doc, dets_json)) {
    for (JsonObjectConst d : doc.as<JsonArrayConst>()) {
      JsonArrayConst b = d["box"];
      if (b.size() != 4) continue;
      CamBox x;
      x.x = (int)(b[0].as<float>() * dw) - ox;
      x.y = (int)(b[1].as<float>() * dh) - oy;
      x.w = (int)(b[2].as<float>() * dw);
      x.h = (int)(b[3].as<float>() * dh);
      x.type = d["type"] | "";
      x.label = String(d["type_name"] | "?") + " · " + String(d["qr"] | "QR ?");
      boxes.push_back(x);
    }
  }
  {
    Lock l;
    box_sets[back] = boxes;
    ready = back;
    problem = "";
  }
  if (++frames % 25 == 1)
    Serial.printf("[cam] frame %u: %u B, wait %u ms, decode %u ms, %u boxes, int free %u\n", (unsigned)frames,
                  (unsigned)len, (unsigned)got_ms, (unsigned)(millis() - t0), (unsigned)boxes.size(),
                  (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL));
  return true;
}

// ---------- reading the response body: raw or HTTP/1.1 chunked ----------
struct BodyReader {
  NetworkClient &c;
  bool chunked;
  size_t left = 0;  // bytes left in the current chunk
  bool eof = false;
  uint32_t timeout_ms = 6000;

  int raw() {  // one byte from the socket, waiting up to timeout_ms
    uint32_t t = millis();
    while (!c.available()) {
      if (!c.connected() || millis() - t > timeout_ms) return -1;
      vTaskDelay(1);
    }
    return c.read();
  }
  String raw_line() {
    String s;
    for (int ch; (ch = raw()) >= 0;) {
      if (ch == '\n') return s;
      if (ch != '\r' && s.length() < 8192) s += (char)ch;
    }
    eof = true;
    return s;
  }
  bool next_chunk() {
    String h = raw_line();
    if (eof) return false;
    if (!h.length()) h = raw_line();  // CRLF after the previous chunk's data
    left = strtoul(h.c_str(), nullptr, 16);
    if (!left) eof = true;
    return left > 0;
  }
  int byte() {
    if (eof) return -1;
    if (chunked && !left && !next_chunk()) return -1;
    int ch = raw();
    if (ch < 0) eof = true;
    else if (chunked) left--;
    return ch;
  }
  size_t bytes(uint8_t *dst, size_t n) {
    size_t got = 0;
    while (got < n && !eof) {
      if (chunked && !left && !next_chunk()) break;
      size_t want = n - got;
      if (chunked && want > left) want = left;
      uint32_t t = millis();
      while (!c.available() && c.connected() && millis() - t < timeout_ms) vTaskDelay(1);
      int r = c.read(dst + got, want);
      if (r <= 0) {
        eof = true;
        break;
      }
      got += r;
      if (chunked) left -= r;
    }
    return got;
  }
  String line() {
    String s;
    for (int ch; (ch = byte()) >= 0;) {
      if (ch == '\n') return s;
      if (ch != '\r' && s.length() < 8192) s += (char)ch;
    }
    return s;
  }
};

// One long request to /api/camera/small/stream: the server pushes tray-only JPEGs (multipart) as they come.
// Returns when the camera is no longer on screen or the stream breaks.
static void run_stream(NetworkClient &client, const String &token) {
  HTTPClient http;
  static const char *hdrs[] = {"Transfer-Encoding"};
  String path = "/api/camera/small/stream?w=" + String(CAM_W) + "&h=" + String(CAM_H) + "&q=65&fps=6";  // decode ~80 ms/frame; 6 fps leaves PSRAM bandwidth for the screen
  const String base = api_base();
  http.begin(client, base + path);
  http.setConnectTimeout(8000);
  http.setTimeout(8000);
  http.addHeader("Authorization", "Bearer " + token);
  http.collectHeaders(hdrs, 1);
  int code = http.GET();
  if (code != 200) {
    if (code > 0) {
      JsonDocument e;
      deserializeJson(e, http.getString());
      set_problem(String(e["error"] | "กล้องไม่ตอบ") + " (" + String(code) + ")");
    } else {
      set_problem("ต่อกล้องไม่ได้ (เครือข่าย)");
    }
    http.end();
    vTaskDelay(pdMS_TO_TICKS(code == 401 ? 1000 : 3000));
    return;
  }
  BodyReader rd{*http.getStreamPtr(), http.header("Transfer-Encoding").equalsIgnoreCase("chunked")};
  Serial.printf("[cam] stream open (%s)\n", rd.chunked ? "chunked" : "raw");
  uint32_t waited = millis();
  while (enabled && !rd.eof && api_base() == base) {  // server changed on the screen: reconnect there
    String l = rd.line();
    if (!l.startsWith("--")) continue;
    size_t len = 0;
    String dets;
    for (;;) {
      String h = rd.line();
      if (rd.eof || !h.length()) break;
      int colon = h.indexOf(':');
      if (colon < 0) continue;
      String k = h.substring(0, colon), v = h.substring(colon + 1);
      v.trim();
      if (k.equalsIgnoreCase("Content-Length")) len = v.toInt();
      else if (k.equalsIgnoreCase("X-Detections")) dets = v;
    }
    if (rd.eof || !len || len > JPG_MAX) break;
    if (rd.bytes(jbuf, len) != len) break;
    publish(len, dets, millis() - waited);
    waited = millis();
  }
  http.end();
  client.stop();  // leaving the stream half-read: start the next one on a fresh connection
  if (enabled) set_problem("ภาพกล้องขาด · กำลังต่อใหม่…");
}

static void cam_task(void *) {
  NetworkClientSecure tls;
  NetworkClient plain;
  tls.setCACert(CA_ROOTS);  // used whenever the server address is https://
  tls.setHandshakeTimeout(15);
  for (;;) {
    String token = api_token();
    if (!enabled || WiFi.status() != WL_CONNECTED || !token.length()) {
      vTaskDelay(pdMS_TO_TICKS(150));
      continue;
    }
    NetworkClient &client = api_base().startsWith("https://") ? (NetworkClient &)tls : plain;
    run_stream(client, token);
  }
}

void cam_begin() {
  mu = xSemaphoreCreateMutex();
  for (int i = 0; i < 3; i++) {
    bufs[i] = (uint16_t *)heap_caps_calloc(CAM_W * CAM_H, 2, MALLOC_CAP_SPIRAM);
    lv_image_dsc_t &d = dscs[i];
    memset(&d, 0, sizeof d);
    d.header.magic = LV_IMAGE_HEADER_MAGIC;
    d.header.cf = LV_COLOR_FORMAT_RGB565;
    d.header.w = CAM_W;
    d.header.h = CAM_H;
    d.header.stride = CAM_W * 2;
    d.data_size = CAM_W * CAM_H * 2;
    d.data = (const uint8_t *)bufs[i];
  }
  void *mem = heap_caps_malloc(sizeof(JPEGDEC), MALLOC_CAP_SPIRAM);
  jpg = new (mem) JPEGDEC();
  jbuf = (uint8_t *)heap_caps_malloc(JPG_MAX, MALLOC_CAP_SPIRAM);
  // its own HTTPS connection (TLS buffers live in PSRAM, see main.cpp), so video never waits for API calls
  xTaskCreatePinnedToCore(cam_task, "kitcheck-cam", 12288, nullptr, 2, nullptr, 0);
}
