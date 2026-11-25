// LVGL 9.2 configuration for the KitCheck kiosk (only the settings that differ from LVGL defaults
// matter; everything else falls back to lv_conf_internal.h).
#ifndef LV_CONF_H
#define LV_CONF_H

#define LV_COLOR_DEPTH 16

/* memory: LVGL's own allocator on a 2.5 MB pool in PSRAM. (The C library malloc keeps every block under
 * 4 KB in internal RAM: widgets and cached Thai glyphs then ate the RAM that WiFi/TLS need.) */
#define LV_USE_STDLIB_MALLOC LV_STDLIB_BUILTIN
#define LV_MEM_SIZE (2560U * 1024U)
#define LV_MEM_POOL_INCLUDE <esp_heap_caps.h>
#define LV_MEM_POOL_ALLOC(size) heap_caps_malloc(size, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT)
#define LV_USE_STDLIB_STRING LV_STDLIB_CLIB
#define LV_USE_STDLIB_SPRINTF LV_STDLIB_CLIB

#define LV_USE_OS LV_OS_NONE
#define LV_DEF_REFR_PERIOD 20
#define LV_DPI_DEF 160

#define LV_USE_LOG 0
#define LV_USE_ASSERT_NULL 1
#define LV_USE_ASSERT_MALLOC 1

/* Thai text: render the IBM Plex Sans Thai TTF at runtime */
#define LV_USE_TINY_TTF 1
#define LV_TINY_TTF_FILE_SUPPORT 0
#define LV_TINY_TTF_CACHE_GLYPH_CNT 256

/* Montserrat only as a fallback for LV_SYMBOL_* icons */
#define LV_FONT_MONTSERRAT_14 1
#define LV_FONT_MONTSERRAT_24 1
#define LV_FONT_MONTSERRAT_48 1
#define LV_FONT_DEFAULT &lv_font_montserrat_14

#define LV_USE_ARC 1
#define LV_USE_SPINNER 1
#define LV_USE_BUTTONMATRIX 1
#define LV_USE_TEXTAREA 1
#define LV_USE_KEYBOARD 1 /* WiFi password entry */
#define LV_USE_LABEL 1
#define LV_LABEL_LONG_TXT_HINT 1

#define LV_USE_THEME_DEFAULT 1
#define LV_THEME_DEFAULT_DARK 0

#endif
