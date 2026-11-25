// Waveshare ESP32-S3-Touch-LCD-5B: 1024x600 ST7262 RGB panel, GT911 touch, CH422G IO expander.
#pragma once
#include <lvgl.h>

constexpr int SCREEN_W = 1024;
constexpr int SCREEN_H = 600;

// Brings up the expander (resets + backlight), the RGB panel and the touch controller,
// then creates the LVGL display and pointer input. Returns false if the panel failed.
bool board_init();
void board_backlight(bool on);
