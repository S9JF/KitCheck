// Live tray camera: a background task pulls /api/camera/snapshot (1280x720 JPEG from ustreamer via
// KitCheck), decodes it at reduced scale into an RGB565 frame of CAM_W x CAM_H (centre crop), and
// fetches CiRA's latest boxes from /api/camera/live. Runs only while a screen shows the camera.
#pragma once
#include <Arduino.h>
#include <lvgl.h>
#include <vector>

constexpr int CAM_W = 592, CAM_H = 333;

struct CamBox {
  int x, y, w, h;  // in frame pixels (already cropped/scaled)
  String type, label;
};

void cam_begin();
void cam_enable(bool on);  // true while a screen shows the camera: the video stream runs only then
// When a newer frame than the last one taken is ready: returns its image (valid until the next
// successful call) and the boxes that go with it.
const lv_image_dsc_t *cam_take_frame(std::vector<CamBox> &boxes);
String cam_problem();  // "" while frames are arriving
