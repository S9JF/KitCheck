// Board bring-up for the Waveshare ESP32-S3-Touch-LCD-5B.
// Pins and timings from esp-arduino-libs/ESP32_Display_Panel BOARD_WAVESHARE_ESP32_S3_TOUCH_LCD_5_B.h.
#include "board.h"

#include <Arduino.h>
#include <Wire.h>
#include <esp_heap_caps.h>
#include <esp_lcd_panel_ops.h>
#include <esp_lcd_panel_rgb.h>

// ---------- I2C bus shared by the touch controller and the IO expander ----------
static constexpr int I2C_SDA = 8, I2C_SCL = 9;
static constexpr int TOUCH_INT = 4;

// ---------- CH422G expander: addressed by function, not by a register ----------
static constexpr uint8_t CH422G_SET = 0x24;  // system parameter: bit0 = IO0-7 as outputs
static constexpr uint8_t CH422G_IO = 0x38;   // write IO0-7
static constexpr int EXIO_TP_RST = 1, EXIO_BL = 2, EXIO_LCD_RST = 3;
static uint8_t exio = 0xFF;

static void exio_write() {
  Wire.beginTransmission(CH422G_IO);
  Wire.write(exio);
  Wire.endTransmission();
}
static void exio_set(int pin, bool high) {
  if (high) exio |= (1 << pin); else exio &= ~(1 << pin);
  exio_write();
}

void board_backlight(bool on) { exio_set(EXIO_BL, on); }

// ---------- RGB panel ----------
static esp_lcd_panel_handle_t panel = nullptr;

static bool panel_init() {
  esp_lcd_rgb_panel_config_t cfg = {};
  cfg.clk_src = LCD_CLK_SRC_DEFAULT;
  // 18 MHz (~21 Hz refresh) instead of 21: the panel reads 14% less from PSRAM, which the camera screen
  // (JPEG decode + redraw) also needs. Starved PSRAM made the picture jump and restart = flicker.
  cfg.timings.pclk_hz = 18 * 1000 * 1000;
  cfg.timings.h_res = SCREEN_W;
  cfg.timings.v_res = SCREEN_H;
  cfg.timings.hsync_pulse_width = 24;
  cfg.timings.hsync_back_porch = 160;
  cfg.timings.hsync_front_porch = 160;
  cfg.timings.vsync_pulse_width = 2;
  cfg.timings.vsync_back_porch = 23;
  cfg.timings.vsync_front_porch = 12;
  cfg.timings.flags.pclk_active_neg = 1;
  cfg.data_width = 16;
  cfg.bits_per_pixel = 16;
  cfg.num_fbs = 2;  // LVGL draws into one while the panel shows the other (no tearing / flicker)
  cfg.bounce_buffer_size_px = SCREEN_W * 15;  // keeps the picture from drifting while PSRAM is busy (2 x 30 KB SRAM)
  cfg.dma_burst_size = 64;
  cfg.hsync_gpio_num = 46;
  cfg.vsync_gpio_num = 3;
  cfg.de_gpio_num = 5;
  cfg.pclk_gpio_num = 7;
  cfg.disp_gpio_num = -1;
  const int data[16] = {14, 38, 18, 17, 10, 39, 0, 45, 48, 47, 21, 1, 2, 42, 41, 40};
  for (int i = 0; i < 16; i++) cfg.data_gpio_nums[i] = data[i];
  cfg.flags.fb_in_psram = 1;
  if (esp_lcd_new_rgb_panel(&cfg, &panel) != ESP_OK) return false;
  if (esp_lcd_panel_reset(panel) != ESP_OK) return false;
  return esp_lcd_panel_init(panel) == ESP_OK;
}

// Direct mode: LVGL renders straight into the panel's back frame buffer. On the last area of a
// refresh we hand that buffer to the panel (a pointer swap, no copy) and wait for the next vsync,
// so LVGL never draws into the buffer that is on screen.
static SemaphoreHandle_t vsync_sem;

static bool IRAM_ATTR on_vsync(esp_lcd_panel_handle_t, const esp_lcd_rgb_panel_event_data_t *, void *) {
  BaseType_t woken = pdFALSE;
  xSemaphoreGiveFromISR(vsync_sem, &woken);
  return woken == pdTRUE;
}

static void flush_cb(lv_display_t *disp, const lv_area_t *, uint8_t *px) {
  if (lv_display_flush_is_last(disp)) {
    xSemaphoreTake(vsync_sem, 0);  // drop a stale vsync
    esp_lcd_panel_draw_bitmap(panel, 0, 0, SCREEN_W, SCREEN_H, px);
    xSemaphoreTake(vsync_sem, pdMS_TO_TICKS(60));
  }
  lv_display_flush_ready(disp);
}

// ---------- GT911 touch ----------
static uint8_t gt911_addr = 0x5D;

static bool gt911_read(uint16_t reg, uint8_t *buf, size_t n) {
  Wire.beginTransmission(gt911_addr);
  Wire.write(reg >> 8);
  Wire.write(reg & 0xFF);
  if (Wire.endTransmission(false) != 0) return false;
  if (Wire.requestFrom((int)gt911_addr, (int)n) != (int)n) return false;
  for (size_t i = 0; i < n; i++) buf[i] = Wire.read();
  return true;
}
static void gt911_write8(uint16_t reg, uint8_t v) {
  Wire.beginTransmission(gt911_addr);
  Wire.write(reg >> 8);
  Wire.write(reg & 0xFF);
  Wire.write(v);
  Wire.endTransmission();
}

static void touch_read_cb(lv_indev_t *, lv_indev_data_t *data) {
  static int16_t lx = 0, ly = 0;
  uint8_t st = 0;
  data->state = LV_INDEV_STATE_RELEASED;
  if (gt911_read(0x814E, &st, 1) && (st & 0x80)) {
    if ((st & 0x0F) > 0) {
      uint8_t p[6];
      if (gt911_read(0x8150, p, 6)) {
        lx = p[0] | (p[1] << 8);
        ly = p[2] | (p[3] << 8);
        data->state = LV_INDEV_STATE_PRESSED;
      }
    }
    gt911_write8(0x814E, 0);
  }
  data->point.x = lx;
  data->point.y = ly;
}

static uint32_t tick_cb() { return millis(); }

bool board_init() {
  Wire.begin(I2C_SDA, I2C_SCL, 400000);

  // expander: all outputs; reset the touch chip with INT held low (-> GT911 address 0x5D) and the LCD
  Wire.beginTransmission(CH422G_SET);
  Wire.write(0x01);
  Wire.endTransmission();
  pinMode(TOUCH_INT, OUTPUT);
  digitalWrite(TOUCH_INT, LOW);
  exio = 0xFF;
  exio &= ~((1 << EXIO_TP_RST) | (1 << EXIO_LCD_RST) | (1 << EXIO_BL));
  exio_write();
  delay(20);
  exio |= (1 << EXIO_TP_RST) | (1 << EXIO_LCD_RST);
  exio_write();
  delay(100);
  pinMode(TOUCH_INT, INPUT);
  delay(100);

  uint8_t id[4] = {0};
  if (!gt911_read(0x8140, id, 4)) {  // product id "911"
    gt911_addr = 0x14;
    gt911_read(0x8140, id, 4);
  }
  Serial.printf("[board] GT911 at 0x%02X id=%c%c%c\n", gt911_addr, id[0], id[1], id[2]);

  if (!panel_init()) {
    Serial.println("[board] RGB panel init FAILED");
    return false;
  }

  lv_init();
  lv_tick_set_cb(tick_cb);
  lv_display_t *disp = lv_display_create(SCREEN_W, SCREEN_H);
  vsync_sem = xSemaphoreCreateBinary();
  esp_lcd_rgb_panel_event_callbacks_t cbs = {};
  cbs.on_vsync = on_vsync;
  esp_lcd_rgb_panel_register_event_callbacks(panel, &cbs, nullptr);
  void *fb0 = nullptr, *fb1 = nullptr;
  esp_lcd_rgb_panel_get_frame_buffer(panel, 2, &fb0, &fb1);
  lv_display_set_color_format(disp, LV_COLOR_FORMAT_RGB565);
  lv_display_set_buffers(disp, fb0, fb1, SCREEN_W * SCREEN_H * 2, LV_DISPLAY_RENDER_MODE_DIRECT);
  lv_display_set_flush_cb(disp, flush_cb);

  lv_indev_t *touch = lv_indev_create();
  lv_indev_set_type(touch, LV_INDEV_TYPE_POINTER);
  lv_indev_set_read_cb(touch, touch_read_cb);

  board_backlight(true);
  return true;
}
