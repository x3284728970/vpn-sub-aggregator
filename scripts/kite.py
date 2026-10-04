#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import base64
import glob
import hashlib
import io
import json
import os
import random
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request

API = "https://api.kitevip.net"
UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
      "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1")
APPID = "189970138"
SALT = b"kitenetwork_salt_2024"
PASSWORD = b"OQIkF0UjbG1jRsdweSX_mUdhKQKVSkm40YSjMETkOJk"

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.environ.get("KITE_OUT") or os.path.join(os.path.dirname(HERE), "output", "kite.txt")
OUT_ENC = OUT + ".enc"
CAPTCHA_HTML = os.environ.get("KITE_CAPTCHA_HTML") or os.path.join(HERE, "kite_captcha.html")


def log(m):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), m), flush=True)


def http_json(url, data=None, timeout=25):
    h = {"Content-Type": "application/json", "User-Agent": UA,
         "Accept": "application/json"}
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers=h,
                                 method="POST" if data is not None else "GET")
    try:
        raw = urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "replace")
        return json.loads(raw) if raw.strip().startswith(("{", "[")) else {"__raw": raw}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return json.loads(raw)
        except Exception:
            return {"__raw": raw[:300], "__http": e.code}
    except Exception as e:
        return {"__err": str(e)[:80]}


def find_chrome():
    c = os.environ.get("KITE_CHROME") or ""
    if c and os.path.exists(c):
        return c
    home = os.path.expanduser("~")
    pats = [
        home + "/.cache/ms-playwright/chromium-*/chrome-linux/chrome",
        home + "/.cache/ms-playwright/chromium_headless_shell-*/chrome-linux/headless_shell",
        "/usr/bin/chromium", "/usr/bin/chromium-browser",
    ]
    for p in pats:
        hits = sorted(glob.glob(p))
        if hits:
            return hits[-1]
    return ""


def write_captcha_html():
    os.makedirs(os.path.dirname(CAPTCHA_HTML), exist_ok=True)
    html = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>cap</title></head>
<body><div id="slot"></div><pre id="status">idle</pre>
<script src="https://ca.turing.captcha.qcloud.com/TJNCaptcha-global.js"></script>
<script>
var APPID = '%s', SLOT = document.getElementById('slot'), STATUS = document.getElementById('status');
window.__net = [];
(function () {
  var _o = XMLHttpRequest.prototype.open, _s = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (m, u) { this.__u = u; return _o.apply(this, arguments); };
  XMLHttpRequest.prototype.send = function (b) {
    var self = this;
    this.addEventListener('load', function () { window.__net.push({ u: String(self.__u).slice(0, 80), resp: String(self.responseText).slice(0, 500) }); });
    return _s.apply(this, arguments);
  };
})();
function build(onResult) {
  try { window.cap.destroy(); } catch (e) {}
  SLOT.innerHTML = '';
  window.cap = new TencentCaptcha(SLOT, APPID, onResult, { userLanguage: 'en' });
  window.cap.show();
}
window.grab = async function () {
  for (var round = 0; round < 4; round++) {
    var res = null;
    window.__net = [];
    build(function (r) { res = r; });
    for (var w = 0; w < 25 && !document.getElementById('tencent-captcha-dy__robot_checkBox_id'); w++) {
      await new Promise(function (r) { setTimeout(r, 200); });
    }
    var parsed = null;
    for (var attempt = 0; attempt < 2 && !parsed; attempt++) {
      var cb = document.getElementById('tencent-captcha-dy__robot_checkBox_id');
      if (cb) { cb.click(); }
      for (var i = 0; i < 16 && !parsed; i++) {
        await new Promise(function (r) { setTimeout(r, 400); });
        var v = (window.__net || []).filter(function (n) { return /verify/.test(n.u); }).pop();
        if (v) {
          var m = v.resp.match(/"ticket":"([^"]+)"/), m2 = v.resp.match(/"randstr":"([^"]+)"/), ec = v.resp.match(/"errorCode":"([^"]+)"/);
          if (m) { parsed = m[1] + '|' + m2[1]; }
          else if (ec && ec[1] !== '0') { parsed = null; break; }
        }
        if (res && res.ticket) { parsed = res.ticket + '|' + res.randstr; }
      }
    }
    if (parsed) { STATUS.textContent = 'TICKET|' + parsed; window.__lastTicket = parsed; return parsed; }
    STATUS.textContent = 'ROUND' + round + '_RETRY';
    await new Promise(function (r) { setTimeout(r, 400); });
  }
  var slider = document.body.innerText.indexOf('puzzle') >= 0;
  STATUS.textContent = slider ? 'SLIDER' : 'FAIL|no-ticket';
  return slider ? 'SLIDER' : 'FAIL|no-ticket';
};
window.tileParams = function () {
  var fg = document.querySelector('.tencent-captcha-dy__fg-item');
  var bgEl = document.querySelector('.tencent-captcha-dy__verify-bg-img');
  var blk = document.querySelector('.tencent-captcha-dy__slider-block');
  if (!fg || !bgEl || !blk) return JSON.stringify({ error: 'no slider dom' });
  return JSON.stringify({ bg: imgURL(bgEl), fg: imgURL(fg), fgPos: getComputedStyle(fg).backgroundPosition, fgBox: box(fg), bgBox: box(bgEl), blkBox: box(blk) });
};
function box(e) { var r = e.getBoundingClientRect(); return [Math.round(r.x), Math.round(r.y), e.clientWidth, e.clientHeight]; }
function imgURL(e) { if (!e) return null; var m = getComputedStyle(e).backgroundImage.match(/url\\("([^"]+)"\\)/); return m ? m[1] : null; }
window.dragSlider = async function (dist) {
  var blk = document.querySelector('.tencent-captcha-dy__slider-block');
  if (!blk) return 'no-slider';
  window.__net = [];
  var r = blk.getBoundingClientRect(), sx = r.x + r.width / 2, sy = r.y + r.height / 2;
  function mk(x, y) { return new Touch({ identifier: 1, target: blk, clientX: x, clientY: y, radiusX: 12, radiusY: 12, force: 0.6 }); }
  function fire(t, x, y) {
    var tp = mk(x, y);
    blk.dispatchEvent(new TouchEvent(t, { bubbles: true, cancelable: true, touches: (t === 'touchend' ? [] : [tp]), targetTouches: (t === 'touchend' ? [] : [tp]), changedTouches: [tp] }));
  }
  fire('touchstart', sx, sy);
  await new Promise(function (r2) { setTimeout(r2, 140); });
  var steps = 42;
  for (var i = 1; i <= steps; i++) {
    var p = i / steps, e = p < 0.8 ? p * 1.05 : 0.84 + (p - 0.8) * 0.8;
    fire('touchmove', sx + dist * Math.min(e, 1), sy + (Math.random() - 0.5) * 1.6);
    await new Promise(function (r2) { setTimeout(r2, p < 0.8 ? 9 : 32); });
  }
  fire('touchend', sx + dist, sy);
  await new Promise(function (r2) { setTimeout(r2, 3200); });
  var v = (window.__net || []).filter(function (n) { return /verify/.test(n.u); }).pop();
  var resp = v ? v.resp : '';
  var m = resp.match(/"ticket":"([^"]+)"/), m2 = resp.match(/"randstr":"([^"]+)"/), ec = resp.match(/"errorCode":"([^"]+)"/);
  if (m) { STATUS.textContent = 'TICKET|' + m[1] + '|RANDSTR|' + m2[1]; return m[1] + '|' + m2[1]; }
  STATUS.textContent = 'DRAG_FAIL|ec=' + (ec && ec[1]);
  return 'FAIL|ec=' + (ec && ec[1]) + '|' + document.body.innerText.slice(-60);
};
</script></body></html>""" % APPID
    open(CAPTCHA_HTML, "w", encoding="utf-8").write(html)


def _download_img(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    return urllib.request.urlopen(req, timeout=timeout).read()


def solve_slider(tp):
    import numpy as np
    from PIL import Image
    from numpy.lib.stride_tricks import sliding_window_view

    A = np.asarray(Image.open(io.BytesIO(_download_img(tp["bg"]))).convert("RGB")).astype(np.float32)
    S = np.asarray(Image.open(io.BytesIO(_download_img(tp["fg"]))).convert("RGBA")).astype(np.float32)
    H, W, _ = A.shape
    m = re.findall(r"(-?\d+(?:\.\d+)?)px", str(tp.get("fgPos", "")))
    scale = 331.361 / 682.0
    if len(m) >= 2:
        x0 = int(round(abs(float(m[0])) / scale)); y0 = int(round(abs(float(m[1])) / scale))
    else:
        x0, y0 = int(round(68.0212 / scale)), int(round(238.074 / scale))
    side = int(round(58 / scale))
    y0 = min(max(y0, 0), S.shape[0] - side); x0 = min(max(x0, 0), S.shape[1] - side)
    alpha = (S[y0:y0 + side, x0:x0 + side, 3] > 128).astype(np.float32)
    g = 0.299 * A[:, :, 0] + 0.587 * A[:, :, 1] + 0.114 * A[:, :, 2]

    def conv2d(img, k):
        v = sliding_window_view(img, (3, 3)); return (v * k).sum(axis=(2, 3))

    kx = np.array([[-1, 0, 1], [-2, 0, 2], [-1, 0, 1]], np.float32)
    ebg = np.hypot(conv2d(g, kx), conv2d(g, kx.T))

    def dil(mask, it=2):
        r = mask.copy()
        for _ in range(it):
            p = np.pad(r, 1)
            r = np.maximum.reduce([p[:-2, :-2], p[:-2, 1:-1], p[:-2, 2:],
                                   p[1:-1, :-2], p[1:-1, 1:-1], p[1:-1, 2:],
                                   p[2:, :-2], p[2:, 1:-1], p[2:, 2:]])
        return r

    outline = dil(alpha, 2) - alpha
    h, w = outline.shape
    F1 = np.fft.rfft2(ebg, s=(H + h - 1, W + w - 1))
    F2 = np.fft.rfft2(outline[::-1, ::-1], s=(H + h - 1, W + w - 1))
    sco = np.fft.irfft2(F1 * F2, s=(H + h - 1, W + w - 1))[h - 1:H, w - 1:W]
    gap_x = int(np.argmax(sco.ravel()) % sco.shape[1])
    return gap_x * (326.0 / W) - 24.0


def grab_ticket(chrome, port):
    import websocket
    args = [chrome, "--headless=new", "--remote-debugging-port=%d" % port,
            "--remote-allow-origins=*",
            "--user-data-dir=%s" % os.path.join(os.path.dirname(CAPTCHA_HTML), "cdp_%d" % port),
            "--no-first-run", "--no-default-browser-check", "--disable-gpu",
            "--no-sandbox", "--disable-dev-shm-usage",
            "--user-agent=%s" % UA, "--disable-blink-features=AutomationControlled"]
    args.append("about:blank")
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(60):
            try:
                urllib.request.urlopen("http://127.0.0.1:%d/json/version" % port, timeout=2).read()
                break
            except Exception:
                time.sleep(0.4)
        else:
            raise RuntimeError("chrome not ready")
        req = urllib.request.Request(
            "http://127.0.0.1:%d/json/new?%s" % (port, urllib.parse.quote("file://" + CAPTCHA_HTML, safe="")),
            method="PUT")
        t = json.loads(urllib.request.urlopen(req, timeout=8).read())
        ws = websocket.create_connection(t["webSocketDebuggerUrl"], timeout=180,
                                         max_size=32 * 1024 * 1024)
        mid = [0]

        def send(method, params=None):
            mid[0] += 1
            ws.send(json.dumps({"id": mid[0], "method": method, "params": params or {}}))
            while True:
                msg = json.loads(ws.recv())
                if msg.get("id") == mid[0]:
                    return msg

        def ev(expr, awaitp=False, timeout=120000):
            r = send("Runtime.evaluate", {"expression": expr, "awaitPromise": awaitp,
                                          "returnByValue": True, "timeout": timeout})
            return r.get("result", {}).get("result", {}).get("value")

        send("Page.enable")
        send("Runtime.enable")
        time.sleep(3.5)
        val = ev("window.grab()", awaitp=True)
        log("ticket: %s" % str(val)[:60])
        if val and "|" in str(val) and "FAIL" not in str(val) and "SLIDER" not in str(val):
            return str(val).split("|", 1)
        if str(val) == "SLIDER":
            for attempt in range(3):
                tp = json.loads(ev("window.tileParams()"))
                if tp.get("error"):
                    raise RuntimeError(str(tp)[:60])
                drag = solve_slider(tp)
                val2 = ev("window.dragSlider(%.1f)" % drag, awaitp=True)
                if val2 and "|" in str(val2) and "FAIL" not in str(val2):
                    return str(val2).split("|", 1)
                time.sleep(1.5)
            raise RuntimeError("slider failed")
        raise RuntimeError("grab failed: %s" % str(val)[:50])
    finally:
        try:
            proc.terminate()
        except Exception:
            pass
        time.sleep(0.5)


def exchange(ticket, randstr):
    r = http_json(API + "/api/captcha/exchange",
                  {"scene": "guest",
                   "payload": {"ticket": ticket, "randstr": randstr},
                   "provider": "tencent"})
    if r.get("ok"):
        return r.get("ticket")
    raise RuntimeError("exchange failed: %s" % json.dumps(r, ensure_ascii=False)[:150])


def guest_login(one_ticket):
    did = "".join(random.choices("0123456789abcdef", k=16))
    body = {"device_id": did, "captcha_ticket": one_ticket, "device_type": "android",
            "device_model": "Pixel 7", "os_version": "14", "app_version": "1.4.0"}
    r = http_json(API + "/auth/guest/login", body)
    return did, r


def derive_key():
    return hashlib.pbkdf2_hmac("sha256", PASSWORD, SALT, 100000, 32)


def decrypt_config(blob):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    s = blob.strip().replace("-", "+").replace("_", "/")
    s = "".join(s.split())
    s += "=" * ((4 - len(s) % 4) % 4)
    raw = base64.b64decode(s)
    return AESGCM(derive_key()).decrypt(raw[:12], raw[12:], None)


def node_uri(o):
    from urllib.parse import quote
    tls = o.get("tls") or {}
    reality = tls.get("reality") or {}
    q = {"encryption": "none", "type": "tcp",
         "sni": tls.get("server_name") or o.get("server") or ""}
    if o.get("flow"):
        q["flow"] = o["flow"]
    if (tls.get("utls") or {}).get("enabled"):
        q["fp"] = (tls["utls"].get("fingerprint") or "") or "chrome"
    if reality.get("enabled"):
        q["security"] = "reality"
        q["pbk"] = reality.get("public_key") or ""
        q["sid"] = reality.get("short_id") or ""
    else:
        q["security"] = "tls"
    qs = "&".join("%s=%s" % (k, quote(str(v), safe="")) for k, v in q.items() if v)
    tag = o.get("tag") or ""
    return "vless://%s@%s:%s?%s#%s" % (o["uuid"], o.get("server"), o.get("server_port"), qs, quote(tag, safe=""))


def encrypt_to(src, dst):
    key_b64 = os.environ.get("KITE_ENC_KEY") or ""
    if not key_b64:
        log("KITE_ENC_KEY not set, skip encrypt")
        return False
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    key = base64.b64decode(key_b64)
    nonce = os.urandom(12)
    data = open(src, "rb").read()
    ct = AESGCM(key).encrypt(nonce, data, None)
    open(dst, "wb").write(nonce + ct)
    log("encrypted %d bytes -> %s" % (len(ct) + 12, dst))
    return True


def main():
    chrome = find_chrome()
    if not chrome:
        log("no chromium found (set KITE_CHROME or playwright install chromium)")
        return 2
    write_captcha_html()
    port = 9260 + random.randint(0, 4)
    tk, rs = grab_ticket(chrome, port)
    log("ticket: %s..." % tk[:35])
    one = exchange(tk, rs)
    did, r = guest_login(one)
    log("guest login code=%s" % r.get("code"))
    blob = r.get("refresh_token") or ""
    if not blob:
        raise RuntimeError("no refresh_token: %s" % json.dumps(r, ensure_ascii=False)[:200])
    cfg = json.loads(decrypt_config(blob).decode())

    uris, seen = [], set()
    for o in cfg.get("outbounds", []):
        if o.get("type") != "vless":
            continue
        try:
            u = node_uri(o)
        except Exception:
            continue
        if not u or u in seen:
            continue
        seen.add(u)
        uris.append(u)
    if not uris:
        raise RuntimeError("no vless nodes parsed")
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(uris) + "\n")
    log("extracted %d vless -> %s" % (len(uris), OUT))
    if encrypt_to(OUT, OUT_ENC):
        log("encrypted copy ready: %s" % OUT_ENC)
    return 0


if __name__ == "__main__":
    sys.exit(main())
