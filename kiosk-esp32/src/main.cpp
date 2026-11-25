// KitCheck self-service kiosk on the Waveshare ESP32-S3-Touch-LCD-5B (1024x600).
// Talks to the KitCheck server (secrets.h: KC_BASE) the same way the web kiosk at /kiosk does.
#include <Arduino.h>
#include <esp_heap_caps.h>
#include <lvgl.h>
#include <mbedtls/platform.h>

#include "api.h"
#include "board.h"
#include "cam.h"
#include "ui.h"

SET_LOOP_TASK_STACK_SIZE(16 * 1024);  // LVGL + TrueType rasterising need more than the default 8 KB

// TLS buffers (~40 KB per connection) go to PSRAM; internal RAM stays free for WiFi and DMA.
static void *tls_calloc(size_t n, size_t size) {
  void *p = heap_caps_calloc(n, size, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
  return p ? p : heap_caps_calloc(n, size, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
}

void setup() {
  mbedtls_platform_set_calloc_free(tls_calloc, free);
  Serial.begin(115200);
  Serial.setTxTimeoutMs(0);  // never block when no serial monitor is attached
  delay(300);
  Serial.printf("\n[kiosk] start, PSRAM %u KB free\n", (unsigned)(ESP.getFreePsram() / 1024));
  if (!board_init()) {
    for (;;) delay(1000);
  }
  api_begin();
  cam_begin();
  ui_begin();
  Serial.println("[kiosk] UI ready");
}

void loop() {
  lv_timer_handler();
  delay(5);
}
