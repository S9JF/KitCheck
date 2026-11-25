/* KitCheck live camera.
   Streams /api/camera/small/stream: the server cuts out the tray and scales it to this view's width before
   sending (MJPEG, ~20-30 KB a frame instead of ~75 KB for the full 1280x720 frame), so the picture stays
   smooth over the Cloudflare tunnel. The login token goes in a header so it never lands in a URL.
   The element is created once and survives page re-renders: after each render the page calls
   LiveCam.mount('<host id>'). When the element is off screen (or the tab is hidden) for 5 s the stream
   stops, to save the server's upload bandwidth. */
(function () {
  'use strict';
  var opt = { token: function () { return null; } };
  var FPS = 30, QUALITY = 65, MAX_W = 960;  // 960 px @ q65 ≈ 17 KB/frame ≈ 4 Mbit/s at 30 fps

  var st = document.createElement('style');
  st.textContent =
    '.livecam{position:absolute;inset:0;background:#1C1E21}' +
    '.livecam canvas{position:absolute;inset:0;width:100%;height:100%}' +
    '.livecam-msg{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;text-align:center;padding:16px;color:#A3A6AD;font-size:16px}' +
    '.livecam-msg:empty{display:none}' +
    '.livecam-badge{position:absolute;left:10px;bottom:10px;background:rgba(28,30,33,.8);color:#EDEBE5;font-size:12px;line-height:1.4;padding:4px 10px;border-radius:999px}' +
    '.livecam-badge .on{color:#7FD69B}.livecam-badge .off{color:#F5A49C}';
  document.head.appendChild(st);

  var el = document.createElement('div');
  el.className = 'livecam';
  var cv = document.createElement('canvas');
  cv.setAttribute('role', 'img');
  cv.setAttribute('aria-label', 'ภาพสดจากกล้องจุดตรวจ');
  var msg = document.createElement('div');
  msg.className = 'livecam-msg';
  msg.setAttribute('role', 'status');
  var badge = document.createElement('div');
  badge.className = 'livecam-badge';
  el.appendChild(cv);
  el.appendChild(msg);
  el.appendChild(badge);
  var ctx = cv.getContext('2d');

  var frame = null, frameAt = 0, startedAt = 0, decoding = false, pending = null;
  var lastErr = '', notConfigured = false;
  var ctrl = null, running = false, retry = null, offSince = 0;

  function attached() { return document.body.contains(el); }
  function setText(node, t) { if (node.textContent !== t) node.textContent = t; }

  /* ---------- MJPEG stream ---------- */
  function streamWidth() {
    var w = Math.round((el.clientWidth || 640) * (window.devicePixelRatio || 1));
    return Math.max(320, Math.min(MAX_W, Math.round(w / 16) * 16));
  }

  function start() {
    retry = null;
    var t = opt.token();
    if (running || !t) return;
    running = true;
    startedAt = Date.now();
    lastErr = '';
    ctrl = new AbortController();
    var url = '/api/camera/small/stream?w=' + streamWidth() + '&q=' + QUALITY + '&fps=' + FPS;
    fetch(url, { headers: { Authorization: 'Bearer ' + t }, signal: ctrl.signal, cache: 'no-store' })
      .then(function (r) {
        if (!r.ok) {
          if (r.status === 503) notConfigured = true;
          return r.json().catch(function () { return {}; }).then(function (j) {
            throw new Error(j.error || ('กล้องตอบผิดพลาด ' + r.status));
          });
        }
        notConfigured = false;
        if (!r.body || !r.body.getReader) throw new Error('เบราว์เซอร์นี้แสดงภาพสดไม่ได้');
        return pump(r.body.getReader());
      })
      .catch(function (e) { if (!e || e.name !== 'AbortError') lastErr = (e && e.message) || String(e); })
      .then(function () {
        running = false;
        ctrl = null;
        if (attached() && !document.hidden && !retry) retry = setTimeout(start, 3000);
        status();
      });
  }

  function stop() {
    if (ctrl) ctrl.abort();
    clearTimeout(retry);
    retry = null;
  }

  var HDR_END = [13, 10, 13, 10];
  function indexOf(buf, pat, from) {
    outer: for (var i = from || 0; i <= buf.length - pat.length; i++) {
      for (var j = 0; j < pat.length; j++) if (buf[i + j] !== pat[j]) continue outer;
      return i;
    }
    return -1;
  }
  function ascii(bytes) {
    var s = '';
    for (var i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i]);
    return s;
  }

  async function pump(reader) {
    var buf = new Uint8Array(0), want = 0, body = 0, partDets = [];  // want/body: length and start of the part being read
    for (;;) {
      var res = await reader.read();
      if (res.done) throw new Error('สตรีมกล้องหยุด');
      var nb = new Uint8Array(buf.length + res.value.length);
      nb.set(buf, 0);
      nb.set(res.value, buf.length);
      buf = nb;
      for (;;) {
        if (!want) {  // look for the next part header (small, at the front of buf)
          var he = indexOf(buf, HDR_END, 0);
          if (he < 0) break;
          var head = ascii(buf.subarray(0, he));
          var m = /content-length:\s*(\d+)/i.exec(head);
          if (!m) { buf = buf.slice(he + 4); continue; }
          want = +m[1];
          body = he + 4;
          var dm = /x-detections:\s*(\[.*\])/i.exec(head);  // AI boxes for this frame (0..1 of the picture)
          try { partDets = dm ? JSON.parse(dm[1]) : []; } catch (e) { partDets = []; }
        }
        if (buf.length < body + want) break;  // wait for the rest of this JPEG without rescanning it
        show(buf.slice(body, body + want), partDets);
        buf = buf.slice(body + want);
        want = 0;
      }
      if (buf.length > 16 * 1024 * 1024) throw new Error('ข้อมูลจากกล้องผิดรูปแบบ');
    }
  }

  var dets = [], pendingDets = [];
  function show(bytes, d) {
    pending = bytes;  // only the newest frame is decoded: a slow device skips frames instead of lagging
    pendingDets = d || [];
    if (!decoding) decodeNext();
  }
  function decodeNext() {
    var b = pending, d = pendingDets;
    pending = null;
    if (!b) return;
    decoding = true;
    createImageBitmap(new Blob([b], { type: 'image/jpeg' }))
      .then(function (bmp) {
        if (frame && frame.close) frame.close();
        frame = bmp;
        dets = d;
        frameAt = Date.now();
        requestAnimationFrame(draw);
        status();
      })
      .catch(function () {})
      .then(function () { decoding = false; if (pending) decodeNext(); });
  }

  /* ---------- drawing ---------- */
  function draw() {
    var w = el.clientWidth, h = el.clientHeight;
    if (!w || !h) return;
    var dpr = window.devicePixelRatio || 1, W = Math.round(w * dpr), H = Math.round(h * dpr);
    if (cv.width !== W || cv.height !== H) { cv.width = W; cv.height = H; }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    if (!frame) return;
    var r = Math.min(w / frame.width, h / frame.height);
    var iw = frame.width * r, ih = frame.height * r, ox = (w - iw) / 2, oy = (h - ih) / 2;
    ctx.drawImage(frame, ox, oy, iw, ih);
    // live AI boxes (real-time detection on the GPU), as 0..1 of the picture
    ctx.lineWidth = 2.5;
    ctx.font = '600 13px system-ui, sans-serif';
    ctx.textBaseline = 'top';
    for (var i = 0; i < dets.length; i++) {
      var d = dets[i], b = d.box;
      if (!b || b.length !== 4) continue;
      var x = ox + b[0] * iw, y = oy + b[1] * ih, bw = b[2] * iw, bh = b[3] * ih;
      var col = d.qr ? '#7FD69B' : '#FFC857';
      ctx.strokeStyle = col;
      ctx.strokeRect(x, y, bw, bh);
      var label = (d.type_name || d.type || '?') + ' ' + Math.round((d.conf || 0) * 100) + '%' + (d.qr ? ' · ' + d.qr : '');
      var tw = ctx.measureText(label).width + 10, ty = y >= 20 ? y - 20 : y;
      ctx.fillStyle = col;
      ctx.fillRect(x, ty, tw, 20);
      ctx.fillStyle = '#1C1E21';
      ctx.fillText(label, x + 5, ty + 3);
    }
  }
  if (window.ResizeObserver) new ResizeObserver(draw).observe(el);

  function status() {
    var stale = frame && Date.now() - frameAt > 4000;
    var m = '';
    if (notConfigured) m = lastErr || 'ยังไม่ได้ตั้งค่ากล้องบนเซิร์ฟเวอร์';
    else if (!frame || stale) m = lastErr ? lastErr + ' · กำลังลองใหม่…' : (stale ? 'ภาพไม่อัปเดต · กำลังต่อใหม่…' : 'กำลังเชื่อมต่อกล้อง…');
    setText(msg, m);
    var b = frame && !stale ? '<span class="on">● กล้องสด</span>' : '<span class="off">● กล้อง</span>';
    if (badge.innerHTML !== b) badge.innerHTML = b;
  }

  setInterval(function () {
    if (!attached() || document.hidden || !opt.token()) {
      if (!offSince) offSince = Date.now();
      else if (Date.now() - offSince > 5000 && (running || retry)) stop();
      return;
    }
    offSince = 0;
    if (!running && !retry) start();
    if (running && Date.now() - Math.max(frameAt, startedAt) > 8000) stop(); // stalled: reconnect
    status();
  }, 500);

  window.LiveCam = {
    init: function (o) { for (var k in o) opt[k] = o[k]; },
    /* Put the live view inside the element with this id (if it exists in the page right now). */
    mount: function (id) {
      var host = document.getElementById(id);
      if (!host) return false;
      if (el.parentNode !== host) host.appendChild(el);
      offSince = 0;
      draw();
      status();
      return true;
    }
  };
})();
