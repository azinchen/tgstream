#!/usr/bin/env python3
"""tgstream capture: watch one Telegram channel, restream its live streams to
MediaMTX and harvest finished streams into the library.

One process per channel. The browser is the distro Chromium driven over the
DevTools protocol (CDP) - no Playwright, no pip dependencies; the only
non-stdlib import is websocket (py3-websocket-client, apk).

Detection is DOM-based (Phase 1) but isolated behind Capture.probe_live() so
it can be swapped for an MTProto detector without touching the join, encode
or harvest logic.
"""

import base64
import datetime
import html
import json
import os
import re
import shlex
import signal
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import websocket


def env(name, default=None, required=False):
    value = os.environ.get(name, "")
    if not value:
        if required:
            raise SystemExit(f"CRITICAL ERROR: {name} is required")
        return default
    return value


CFG = {
    "slug": env("TG_SLUG", required=True),
    "peer": env("TG_PEER", required=True),
    "name": env("TG_NAME") or env("TG_SLUG", required=True),
    "channel_number": int(env("TG_CHANNEL_NUMBER", "1")),
    "width": int(env("CAPTURE_WIDTH", "1920")),
    "height": int(env("CAPTURE_HEIGHT", "1080")),
    "fps": int(env("CAPTURE_FPS", "30")),
    "bitrate": env("CAPTURE_BITRATE", "4500k"),
    "encoder": env("CAPTURE_ENCODER", "cpu"),
    "vaapi_device": env("VAAPI_DEVICE", "/dev/dri/renderD128"),
    # Residual constant A/V offset in seconds; positive delays audio.
    "audio_offset": float(env("AUDIO_OFFSET", "0")),
    "record": env("RECORD", "true").lower() == "true",
    "poll_interval": float(env("POLL_INTERVAL", "5")),
    "end_grace": float(env("END_GRACE", "45")),
    "join_timeout": float(env("JOIN_TIMEOUT", "45")),
    "rtmp_url": env("RTMP_URL", "rtmp://mediamtx:1935"),
    "public_host": env("PUBLIC_HOST", "localhost"),
    "hls_port": int(env("HLS_PORT", "8888")),
    "http_port": int(env("HTTP_PORT", "8409")),
    # Host-published port for the /login URL printed in the log, when the
    # compose mapping differs from HTTP_PORT (e.g. "8410:8409").
    "public_http_port": int(env("PUBLIC_HTTP_PORT", env("HTTP_PORT", "8409"))),
    "display": env("DISPLAY", ":99"),
    "cdp_port": int(env("CDP_PORT", "9222")),
    "segment_seconds": int(env("RECORD_SEGMENT_SECONDS", "600")),
    "segments_dir": env("SEGMENTS_DIR", "/segments"),
    "library_dir": env("LIBRARY_DIR", "/library"),
    "state_dir": env("STATE_DIR", "/state"),
}

PATH = f"tg-{CFG['slug']}"
STREAM_URL = f"http://{CFG['public_host']}:{CFG['hls_port']}/{PATH}/index.m3u8"
PUBLISH_URL = f"{CFG['rtmp_url']}/{PATH}"

XMLTV_FMT = "%Y%m%d%H%M%S %z"


def log(msg):
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{stamp} [CAPTURE] {msg}", flush=True)


# ---------------------------------------------------------------------------
# FRAGILE: Telegram Web K selectors and visible-text probes.
#
# Everything that depends on Telegram Web's DOM lives between these markers.
# When Telegram renames things, refresh this block only: run the container
# with DEBUG_VNC=true, start a group call in a throwaway channel you own, and
# inspect the live bar. Russian strings included because the account's UI
# language is likely Russian. Video handling below is deliberately
# selector-free and should NOT need updates.
# ---------------------------------------------------------------------------

LIVE_BAR_SELECTORS = [
    # RTMP live streams (verified 2026-08 against a real broadcast): bar text
    # "Live Stream / N watching / Join", join button below.
    ".pinned-container.pinned-live",
    # Video chats / group calls (K can join these but renders audio only).
    ".pinned-container.pinned-group-call",
    ".pinned-container.pinned-call",
]

# The explicit Join control inside the live bar, when present.
LIVE_JOIN_BUTTON = ".pinned-live-action-button"

LIVE_TEXT_MARKERS = [
    "live stream",
    "video chat",
    "voice chat",
    "прямой эфир",
    "трансляция",
    "голосовой чат",
    "видеочат",
]

JOIN_BUTTON_TEXTS = [
    "join",
    "watch",
    "присоединиться",
    "смотреть",
]

# Telegram Web K keeps the MTProto session under this localStorage key; its
# presence is the authorization signal for the integrated QR login.
AUTH_PROBE_JS = """
(() => {
    try {
        return { authorized: !!localStorage.getItem('user_auth') };
    } catch (e) {
        return { authorized: false };
    }
})()
"""

# 2FA: after a QR scan, accounts with a cloud password get a password form.
PASSWORD_PRESENT_JS = """
(() => {
    for (const el of document.querySelectorAll('input[type="password"]')) {
        const r = el.getBoundingClientRect();
        if (r.width > 5 && r.height > 5) return true;
    }
    return false;
})()
"""

SUBMIT_PASSWORD_JS = """
((pw) => {
    let input = null;
    for (const el of document.querySelectorAll('input[type="password"]')) {
        const r = el.getBoundingClientRect();
        if (r.width > 5 && r.height > 5) { input = el; break; }
    }
    if (!input) return false;
    const setter = Object.getOwnPropertyDescriptor(
        window.HTMLInputElement.prototype, 'value').set;
    setter.call(input, pw);
    input.dispatchEvent(new Event('input', {bubbles: true}));
    // Prefer a visible submit button; fall back to Enter on the input.
    let clicked = false;
    for (const b of document.querySelectorAll('button')) {
        const r = b.getBoundingClientRect();
        if (r.width < 5 || r.height < 5) continue;
        const t = (b.innerText || '').trim().toLowerCase();
        if (t && t.length < 30) { b.click(); clicked = true; break; }
    }
    if (!clicked) {
        input.dispatchEvent(new KeyboardEvent('keydown',
            {key: 'Enter', code: 'Enter', keyCode: 13, bubbles: true}));
    }
    return true;
})(__PASSWORD__)
"""

# Probe the active chat's top area for a visible live-stream bar. Selector
# hits win; otherwise fall back to scanning visible text for the markers.
# Marks the found element with data-tgstream-live so join() can click it.
PROBE_JS = """
(() => {
    const selectors = __SELECTORS__;
    const markers = __MARKERS__;
    // Scan only the active chat column: the sidebar previews service
    // messages ("Live Stream started") that must not trigger detection.
    const scope = document.querySelector('#column-center') || document.body;
    const visible = (el) => {
        const r = el.getBoundingClientRect();
        if (r.width < 10 || r.height < 10) return false;
        const s = window.getComputedStyle(el);
        return s.display !== 'none' && s.visibility !== 'hidden';
    };
    const mark = (el, title) => {
        document.querySelectorAll('[data-tgstream-live]')
            .forEach(e => e.removeAttribute('data-tgstream-live'));
        el.setAttribute('data-tgstream-live', '1');
        return { live: true, title: title || '' };
    };
    for (const sel of selectors) {
        try {
            for (const el of scope.querySelectorAll(sel)) {
                if (visible(el)) return mark(el, el.innerText.split('\\n')[0]);
            }
        } catch (e) { /* bad selector after UI churn - fall through */ }
    }
    // Text fallback: any visible element near the top of the chat column
    // whose own text matches a live marker.
    const lower = markers.map(m => m.toLowerCase());
    const all = scope.querySelectorAll('div,section,button,span');
    for (const el of all) {
        const r = el.getBoundingClientRect();
        if (r.top > 250 || !visible(el)) continue;
        const text = (el.innerText || '').trim().toLowerCase();
        if (!text || text.length > 120) continue;
        if (lower.some(m => text.includes(m))) {
            // Prefer a clickable ancestor if the match is a bare label.
            let target = el;
            for (let up = el; up && up !== scope; up = up.parentElement) {
                const cls = up.className || '';
                if (typeof cls === 'string' &&
                    (cls.includes('pinned') || up.tagName === 'BUTTON')) {
                    target = up;
                    break;
                }
            }
            return mark(target, el.innerText.split('\\n').pop());
        }
    }
    return { live: false, title: '' };
})()
""".replace("__SELECTORS__", json.dumps(LIVE_BAR_SELECTORS)) \
   .replace("__MARKERS__", json.dumps(LIVE_TEXT_MARKERS))

CLICK_LIVE_BAR_JS = """
(() => {
    // Prefer the live bar's explicit Join control. Never blind-click an
    // in-call bar (pinned-call) - that area holds mic/leave buttons.
    const btn = document.querySelector('__JOIN_BUTTON__');
    if (btn && btn.getBoundingClientRect().width > 0) {
        btn.click();
        return 'join';
    }
    const el = document.querySelector('[data-tgstream-live]');
    if (el && !(el.className || '').toString().includes('pinned-call')) {
        el.click();
        return 'bar';
    }
    return 'already joined';
})()
""".replace("__JOIN_BUTTON__", LIVE_JOIN_BUTTON)

# After joining an RTMP stream the in-call bar appears but the stream player
# (and its <video>) only materializes when the bar is clicked open. The click
# TOGGLES the player, so it must never fire while the player exists (even
# one still loading) - the guard checks for player videos, not readiness.
OPEN_PLAYER_JS = """
(() => {
    // Only the player's own video (media-video class) proves the player is
    // open - the live bar carries a circular preview video that must not
    // satisfy this check.
    if (document.querySelector('video.media-video'))
        return 'player present';
    const c = document.querySelector('.topbar-call-center');
    if (!c) return 'no call bar';
    c.click();
    return 'clicked';
})()
"""

# A previous failed attempt can leave the account joined to the call
# server-side; K re-attaches on boot and shows a stuck "Connecting..." bar,
# and never auto-opens the player for a re-attached call. Leave it first.
LEAVE_STUCK_CALL_JS = """
(() => {
    const bar = document.querySelector('.pinned-call');
    if (!bar || bar.getBoundingClientRect().width === 0) return 'no call';
    const btn = bar.querySelector('.topbar-call-end-btn');
    if (!btn) return 'no leave button';
    btn.click();
    return 'left';
})()
"""

# Confirm buttons inside popups only ("Join"/"Watch" dialogs) - a global
# button sweep re-clicks the live bar's own Join and toggles the call.
CLICK_JOIN_JS = """
(() => {
    const texts = __TEXTS__;
    const lower = texts.map(t => t.toLowerCase());
    const candidates = document.querySelectorAll(
        '.popup button, .popup .btn-primary');
    for (const el of candidates) {
        const r = el.getBoundingClientRect();
        if (r.width < 5 || r.height < 5) continue;
        const text = (el.innerText || '').trim().toLowerCase();
        if (text && text.length < 40 && lower.some(t => text.includes(t))) {
            el.click();
            return text;
        }
    }
    return null;
})()
""".replace("__TEXTS__", json.dumps(JOIN_BUTTON_TEXTS))

# ---------------------------------------------------------------------------
# End of the fragile selector block.
# ---------------------------------------------------------------------------

# QR login mirroring. The login QR is a square-ish canvas (the biggest canvas
# on the page is the doodle wallpaper, hence the aspect-ratio filter) with a
# transparent background (composited onto white or it decodes as black).
# jsQR is vendored next to this file and injected on demand, because SPA
# navigations recreate the JS context and drop injected globals.

with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "jsQR.js")) as _f:
    JSQR_SRC = _f.read()

QR_MIRROR_JS = """
(() => {
    if (typeof jsQR === 'undefined') return {err: 'jsqr missing'};
    const all = [...document.querySelectorAll('canvas')]
        .filter(c => c.width > 50);
    if (!all.length) return {err: 'no canvas'};
    const squarish = all.filter(c => {
        const ratio = c.width / c.height;
        return ratio > 0.8 && ratio < 1.25;
    }).sort((a, b) => b.width * b.height - a.width * a.height);
    if (!squarish.length) return {err: 'no square canvas'};
    for (const c of squarish) {
        const off = document.createElement('canvas');
        off.width = c.width; off.height = c.height;
        const ctx = off.getContext('2d', {willReadFrequently: true});
        ctx.fillStyle = '#fff';
        ctx.fillRect(0, 0, off.width, off.height);
        ctx.drawImage(c, 0, 0);
        const d = ctx.getImageData(0, 0, off.width, off.height);
        const r = jsQR(d.data, d.width, d.height,
                       {inversionAttempts: 'attemptBoth'});
        if (r && r.data) {
            return {data: r.data, png: off.toDataURL('image/png')};
        }
    }
    return {err: 'decode failed'};
})()
"""

# Selector-free video handling: pick the best loaded <video>, reparent it
# into a full-viewport stage and start playback. K's stream player opens
# PAUSED (readyState 4, paused:true), so paused videos count. Scoring:
# the player's main video carries the media-video class; square videos are
# circular participant/preview thumbnails and must lose to the real stream.
# Idempotent and re-entrant: called every live tick, so when the main video
# loads late it evicts a provisionally staged thumbnail.
STAGE_VIDEO_JS = """
(() => {
    const loaded = [...document.querySelectorAll('video')].filter(v => {
        return v.readyState >= 2 && v.videoWidth > 0;
    });
    if (!loaded.length) return false;
    const main = loaded.filter(v =>
        (v.className || '').toString().includes('media-video'));
    let pool;
    if (main.length) {
        pool = main;
    } else if (document.querySelector('.pinned-call.is-rtmp') ||
               document.querySelector('.pinned-live')) {
        // RTMP stream: without the player's media-video, the only loaded
        // videos are circular bar previews - wait for the real player.
        return false;
    } else {
        pool = loaded;  // video chats have no media-video; take what exists
    }
    const score = (v) => {
        let s = v.videoWidth * v.videoHeight;
        if (v.videoWidth === v.videoHeight) s -= 50000000;
        return s;
    };
    pool.sort((a, b) => score(b) - score(a));
    const v = pool[0];
    let stage = document.getElementById('tgstream-stage');
    if (!stage) {
        stage = document.createElement('div');
        stage.id = 'tgstream-stage';
        stage.style.cssText =
            'position:fixed;inset:0;background:#000;z-index:2147483647;' +
            'display:flex;align-items:center;justify-content:center;cursor:none;';
        document.body.appendChild(stage);
    }
    if (v.parentElement !== stage) {
        // Evict a previously staged (lesser) video; keep it alive but hidden.
        for (const old of [...stage.children]) {
            old.style.cssText = 'display:none;';
            document.body.appendChild(old);
        }
        stage.appendChild(v);
    }
    v.style.cssText = 'width:100vw;height:100vh;object-fit:contain;cursor:none;';
    v.muted = false;
    v.volume = 1.0;
    v.controls = false;
    // Telegram live streams are rewindable and the player opens at the
    // buffer position, not the live edge - which can be hours behind.
    // Seek to the live edge; the 15s guard re-seeks only on real lag.
    try {
        if (v.seekable && v.seekable.length) {
            const edge = v.seekable.end(v.seekable.length - 1);
            if (edge - v.currentTime > 15) v.currentTime = Math.max(0, edge - 2);
        }
    } catch (e) {}
    v.play().catch(() => {});
    return true;
})()
"""

UNSTAGE_JS = """
(() => {
    const stage = document.getElementById('tgstream-stage');
    if (stage) stage.remove();
})()
"""

VIDEO_CLOCK_JS = """
(() => {
    const v = document.querySelector('#tgstream-stage video');
    if (!v) return null;
    return { t: v.currentTime, ended: v.ended, paused: v.paused };
})()
"""


def sanitize_title(title):
    title = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", title).strip().rstrip(".")
    return title[:120] or "Stream"


class State:
    """Shared status, read by the HTTP server, written by the capture loop."""

    def __init__(self):
        self.lock = threading.Lock()
        self.state = "starting"
        self.title = None
        self.since = None
        self.last_error = None
        # Integrated QR login: written by the capture loop, read by HTTP;
        # pending_password flows the other way (POST /login/password).
        self.qr_png = None
        self.qr_token = None
        self.password_needed = False
        self.pending_password = None

    def set_qr(self, token, png):
        """Store the current QR; True if the token changed."""
        with self.lock:
            changed = token != self.qr_token
            self.qr_token = token
            self.qr_png = png
            return changed

    def clear_login(self):
        with self.lock:
            self.qr_png = None
            self.qr_token = None
            self.password_needed = False
            self.pending_password = None

    def take_password(self):
        with self.lock:
            pw = self.pending_password
            self.pending_password = None
            return pw

    def set(self, state=None, title=None, error=None):
        with self.lock:
            if state is not None and state != self.state:
                self.state = state
                self.since = datetime.datetime.now(datetime.timezone.utc)
                log(f"State -> {state}" + (f" ({title})" if title else ""))
            if title is not None:
                self.title = title or None
            if error is not None:
                self.last_error = error
                log(f"Error: {error}")

    def snapshot(self):
        with self.lock:
            return {
                "slug": CFG["slug"],
                "name": CFG["name"],
                "path": PATH,
                "channel_number": CFG["channel_number"],
                "state": self.state,
                "title": self.title,
                "since": self.since.isoformat() if self.since else None,
                "since_ts": self.since.timestamp() if self.since else None,
                "url": STREAM_URL,
                "record": CFG["record"],
                "last_error": self.last_error,
            }


STATE = State()


# ---------------------------------------------------------------------------
# Browser: distro Chromium driven over CDP
# ---------------------------------------------------------------------------

class BrowserError(Exception):
    pass


class Browser:
    """Launches Chromium on the Xvfb display and talks CDP to its page.

    The whole automation surface is three methods: navigate, reload and
    evaluate. Every failure raises BrowserError; the caller relaunches.
    """

    def __init__(self):
        self.proc = None
        self.ws = None
        self.msg_id = 0

    def launch(self, url):
        profile = os.path.join(CFG["state_dir"], "profile")
        # A lock left by a previous container carries a foreign hostname, and
        # Chromium refuses profiles "in use on another computer" (exit 21).
        # We are the profile's only user, so clearing it is always safe.
        for lock in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            try:
                os.unlink(os.path.join(profile, lock))
            except OSError:
                pass
        args = [
            "chromium-browser",
            f"--user-data-dir={profile}",
            f"--remote-debugging-port={CFG['cdp_port']}",
            f"--remote-allow-origins=http://127.0.0.1:{CFG['cdp_port']}",
            "--kiosk",
            f"--window-size={CFG['width']},{CFG['height']}",
            "--window-position=0,0",
            "--autoplay-policy=no-user-gesture-required",
            "--no-first-run",
            "--disable-infobars",
            "--disable-session-crashed-bubble",
            "--hide-crash-restore-bubble",
            "--no-sandbox",
            url,
        ]
        log(f"Launching browser: {url}")
        self.proc = subprocess.Popen(
            args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._connect()

    def _connect(self):
        base = f"http://127.0.0.1:{CFG['cdp_port']}"
        deadline = time.monotonic() + 60
        targets = None
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise BrowserError(
                    f"chromium exited with code {self.proc.returncode}")
            try:
                with urllib.request.urlopen(f"{base}/json", timeout=5) as resp:
                    targets = json.load(resp)
                break
            except OSError:
                time.sleep(1)
        if targets is None:
            raise BrowserError("CDP endpoint never came up")
        pages = [t for t in targets if t.get("type") == "page"]
        if not pages:
            raise BrowserError("no page target in CDP target list")
        # Chrome 111+ rejects CDP connections with a foreign Origin header;
        # send none (suppress_origin) and allow the local one via the
        # --remote-allow-origins flag as a fallback.
        self.ws = websocket.create_connection(
            pages[0]["webSocketDebuggerUrl"], timeout=30,
            suppress_origin=True)

    def cmd(self, method, params=None, timeout=30):
        if self.ws is None:
            raise BrowserError("not connected")
        self.msg_id += 1
        try:
            self.ws.send(json.dumps(
                {"id": self.msg_id, "method": method, "params": params or {}}))
            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise BrowserError(f"{method} timed out")
                self.ws.settimeout(remaining)
                msg = json.loads(self.ws.recv())
                # CDP interleaves async events with responses; match by id.
                if msg.get("id") == self.msg_id:
                    if "error" in msg:
                        raise BrowserError(f"{method}: {msg['error']}")
                    return msg.get("result", {})
        except (websocket.WebSocketException, OSError, ValueError) as exc:
            raise BrowserError(f"{method}: {exc}") from exc

    def evaluate(self, expression):
        result = self.cmd("Runtime.evaluate", {
            "expression": expression, "returnByValue": True})
        if "exceptionDetails" in result:
            detail = result["exceptionDetails"].get("text", "JS exception")
            raise BrowserError(f"evaluate: {detail}")
        return result.get("result", {}).get("value")

    def navigate(self, url):
        self.cmd("Page.navigate", {"url": url})

    def reload(self):
        self.cmd("Page.reload", {})

    def close(self):
        if self.ws is not None:
            try:
                self.ws.close()
            except (websocket.WebSocketException, OSError):
                pass
            self.ws = None
        if self.proc is not None:
            if self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait()
            self.proc = None


# ---------------------------------------------------------------------------
# ffmpeg supervisor
# ---------------------------------------------------------------------------

class Ffmpeg:
    """Runs exactly one publisher (slate or live) on the channel's RTMP path.

    ensure() is called every tick from the main loop: it restarts a dead
    process in its current mode and switches modes by stopping and starting.
    """

    def __init__(self):
        self.proc = None
        self.mode = None

    def command(self, mode):
        if mode == "slate":
            return [
                "ffmpeg", "-hide_banner", "-loglevel", "warning",
                "-re", "-stream_loop", "-1",
                "-i", os.path.join(CFG["state_dir"], "slate.ts"),
                "-c", "copy",
                "-f", "flv", PUBLISH_URL,
            ]
        # A/V sync: thread_queue_size stops input-queue starvation under
        # encoder load (a classic drift source); small pulse fragments cut
        # audio buffering latency; aresample=async=1 absorbs residual drift.
        # Do NOT wallclock-stamp the pulse input - its packet read times are
        # jittery and async resampling then pads the jitter with silence.
        # AUDIO_OFFSET shifts audio for any remaining constant offset.
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "warning",
            "-thread_queue_size", "1024",
            "-f", "x11grab",
            "-framerate", str(CFG["fps"]),
            "-video_size", f"{CFG['width']}x{CFG['height']}",
            "-draw_mouse", "0",
            "-i", CFG["display"],
            "-thread_queue_size", "1024",
        ]
        if CFG["audio_offset"] != 0.0:
            cmd += ["-itsoffset", str(CFG["audio_offset"])]
        cmd += [
            "-f", "pulse", "-fragment_size", "4096", "-i", "tgcap.monitor",
        ]
        gop = str(CFG["fps"] * 2)
        if CFG["encoder"] == "vaapi":
            cmd += [
                "-vaapi_device", CFG["vaapi_device"],
                "-vf", "format=nv12,hwupload",
                "-c:v", "h264_vaapi",
                "-b:v", CFG["bitrate"], "-maxrate", CFG["bitrate"],
                "-g", gop,
            ]
        elif CFG["encoder"] == "v4l2m2m":
            # Raspberry Pi 4 hardware encoder (Pi 5 has no H.264 encoder).
            cmd += [
                "-pix_fmt", "yuv420p",
                "-c:v", "h264_v4l2m2m",
                "-b:v", CFG["bitrate"],
                "-g", gop,
            ]
        else:
            cmd += [
                "-c:v", "libx264", "-preset", "veryfast",
                "-profile:v", "high", "-pix_fmt", "yuv420p",
                "-b:v", CFG["bitrate"], "-maxrate", CFG["bitrate"],
                "-bufsize", "2M",
                "-g", gop,
            ]
        cmd += [
            # min_hard_comp keeps async correction to rare hard jumps -
            # continuous micro-adjustment creates backward DTS steps that
            # make MediaMTX drop readers/recorder.
            "-af", "aresample=async=1:min_hard_comp=0.100:first_pts=0",
            "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "128k",
            "-f", "flv", PUBLISH_URL,
        ]
        return cmd

    def ensure(self, mode):
        if self.proc is not None and self.proc.poll() is not None:
            log(f"ffmpeg ({self.mode}) died with code {self.proc.returncode}, restarting")
            self.proc = None
        if self.proc is not None and self.mode == mode:
            return
        self.stop()
        cmd = self.command(mode)
        log(f"Starting ffmpeg ({mode}): {shlex.join(cmd)}")
        self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL)
        self.mode = mode

    def stop(self):
        if self.proc is None:
            return
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self.proc = None
        self.mode = None


# ---------------------------------------------------------------------------
# Harvest: concatenate MediaMTX segments overlapping the live window
# ---------------------------------------------------------------------------

SEGMENT_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})_(\d{2})-(\d{2})-(\d{2})-\d+\.ts$")


def harvest(title, start_ts, end_ts):
    """Copy-concatenate the recording segments covering [start_ts, end_ts]
    into the library. Runs in a background thread; must not touch STATE
    machine internals beyond last_error."""
    try:
        seg_dir = os.path.join(CFG["segments_dir"], PATH)
        if not os.path.isdir(seg_dir):
            raise RuntimeError(f"segment directory {seg_dir} does not exist")

        margin = 15
        seg_len = CFG["segment_seconds"]
        chosen = []
        for fname in sorted(os.listdir(seg_dir)):
            m = SEGMENT_RE.match(fname)
            if not m:
                continue
            # Segment timestamps are local time (must share TZ with MediaMTX).
            seg_start = datetime.datetime(
                *(int(g) for g in m.groups())).timestamp()
            if seg_start < end_ts + margin and seg_start + seg_len > start_ts - margin:
                chosen.append(os.path.join(seg_dir, fname))
        if not chosen:
            raise RuntimeError("no recording segments overlap the live window")

        date = datetime.datetime.fromtimestamp(start_ts).strftime("%Y-%m-%d")
        out_dir = os.path.join(CFG["library_dir"], CFG["name"])
        os.makedirs(out_dir, exist_ok=True)
        base = f"{CFG['name']} - {date} - {sanitize_title(title)}"
        out = os.path.join(out_dir, f"{base}.mp4")
        n = 2
        while os.path.exists(out):
            out = os.path.join(out_dir, f"{base} ({n}).mp4")
            n += 1

        concat_list = os.path.join(
            CFG["state_dir"], f"harvest-concat-{int(start_ts)}.txt")
        with open(concat_list, "w") as f:
            for path in chosen:
                escaped = path.replace("'", "'\\''")
                f.write(f"file '{escaped}'\n")

        log(f"Harvesting {len(chosen)} segments -> {out}")
        tmp = out + ".part"
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
             "-f", "concat", "-safe", "0", "-i", concat_list,
             "-c", "copy", "-movflags", "+faststart", "-f", "mp4", tmp],
            check=True)
        os.rename(tmp, out)
        os.unlink(concat_list)
        log(f"Harvest complete: {out}")
    except Exception as exc:  # noqa: BLE001 - report, never crash the capture
        STATE.set(error=f"harvest failed: {exc}")


# ---------------------------------------------------------------------------
# HTTP endpoints (stdlib, no framework)
# ---------------------------------------------------------------------------

def render_playlist():
    s = STATE.snapshot()
    return "\n".join([
        "#EXTM3U",
        (f'#EXTINF:-1 tvg-id="{s["slug"]}" tvg-name="{s["name"]}" '
         f'tvg-chno="{s["channel_number"]}" group-title="Telegram",{s["name"]}'),
        s["url"],
        "",
    ])


def render_epg():
    s = STATE.snapshot()
    now = datetime.datetime.now(datetime.timezone.utc).replace(
        minute=0, second=0, microsecond=0)
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<tv generator-info-name="tgstream">']
    out.append(f'  <channel id="{s["slug"]}">')
    out.append(f'    <display-name>{html.escape(s["name"])}</display-name>')
    out.append('  </channel>')

    def programme(start, stop, title):
        out.append(f'  <programme start="{start.strftime(XMLTV_FMT)}" '
                   f'stop="{stop.strftime(XMLTV_FMT)}" channel="{s["slug"]}">')
        out.append(f'    <title>{html.escape(title)}</title>')
        out.append('  </programme>')

    if s["state"] == "live":
        since = datetime.datetime.fromisoformat(s["since"])
        programme(since, now + datetime.timedelta(hours=4),
                  s["title"] or f"{s['name']} live")
    else:
        # Plex refuses channels without guide data, so fill with hour blocks.
        for i in range(12):
            programme(now + datetime.timedelta(hours=i),
                      now + datetime.timedelta(hours=i + 1),
                      f"{s['name']} (no stream)")
    out.append('</tv>')
    return "\n".join(out)


def render_login_page():
    s = STATE.snapshot()
    if s["state"] != "needs-login":
        body = ("<p>✓ Logged in — nothing to do here.</p>"
                f"<p>Channel state: <b>{html.escape(s['state'])}</b></p>")
        refresh = 30
    else:
        with STATE.lock:
            password_needed = STATE.password_needed
            token = STATE.qr_token
        if password_needed:
            body = (
                "<p>QR scanned. This account has a cloud password (2FA) — "
                "enter it to finish:</p>"
                '<form method="post" action="/login/password">'
                '<input type="password" name="password" autofocus> '
                '<button type="submit">Submit</button></form>')
            refresh = 0
        elif token:
            body = (
                "<p>Scan with the Telegram app: "
                "<b>Settings &rarr; Devices &rarr; Link Desktop Device</b></p>"
                '<p><img src="/qr.png" width="300" height="300" '
                'style="image-rendering:pixelated"></p>'
                f"<p>Can't scan? Open this on the phone: "
                f"<code>{html.escape(token)}</code></p>"
                "<p>The code rotates every ~30 seconds; this page follows.</p>")
            refresh = 2
        else:
            body = "<p>Waiting for Telegram Web to show the QR code…</p>"
            refresh = 2
    meta = (f'<meta http-equiv="refresh" content="{refresh}">'
            if refresh else "")
    return (f"<!doctype html><html><head><title>{html.escape(CFG['name'])}"
            f" login</title>{meta}</head>"
            f"<body style=\"font-family:sans-serif;max-width:40em;margin:2em auto\">"
            f"<h2>tgstream: {html.escape(CFG['name'])}</h2>{body}</body></html>")


class Handler(BaseHTTPRequestHandler):
    def _send(self, body, ctype, code=200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/qr.png":
            with STATE.lock:
                png = STATE.qr_png
            if png is None:
                self.send_error(404)
            else:
                self._send(png, "image/png")
            return
        if path == "/login":
            self._send(render_login_page().encode(), "text/html")
            return
        routes = {
            "/status": (lambda: json.dumps(STATE.snapshot()),
                        "application/json"),
            "/playlist.m3u": (render_playlist, "audio/x-mpegurl"),
            "/epg.xml": (render_epg, "application/xml"),
        }
        route = routes.get(path)
        if route is None:
            self.send_error(404)
            return
        self._send(route[0]().encode(), route[1])

    def do_POST(self):
        if self.path.split("?")[0] != "/login/password":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", 0))
        form = urllib.parse.parse_qs(self.rfile.read(length).decode())
        password = (form.get("password") or [""])[0]
        if password:
            with STATE.lock:
                STATE.pending_password = password
        # Redirect back; the capture loop types the password into the page.
        self.send_response(303)
        self.send_header("Location", "/login")
        self.end_headers()

    def log_message(self, fmt, *args):  # keep the service log readable
        pass


# ---------------------------------------------------------------------------
# Capture loop
# ---------------------------------------------------------------------------

class Capture:
    def __init__(self):
        self.ffmpeg = Ffmpeg()
        self.browser = None
        self.live_start = None
        self.live_title = None
        self.last_video_ok = None
        self.last_clock = None
        self.join_failures = 0
        self.last_join_attempt = 0.0

    def channel_url(self):
        peer = CFG["peer"].strip()
        if "t.me/+" in peer or "joinchat/" in peer:
            invite = peer.rstrip("/").split("+")[-1] if "/+" in peer \
                else peer.rstrip("/").split("joinchat/")[-1]
            return ("https://web.telegram.org/k/#?tgaddr="
                    f"tg%3A%2F%2Fjoin%3Finvite%3D{invite}")
        if "t.me/" in peer:
            peer = "@" + peer.rstrip("/").split("t.me/")[-1]
        if peer.startswith("@"):
            return f"https://web.telegram.org/k/#{peer}"
        # Bare numeric channel id, e.g. -1001234567890.
        return f"https://web.telegram.org/k/#{peer}"

    # -- integrated QR login --------------------------------------------------

    def check_authorized(self):
        return bool(self.browser.evaluate(AUTH_PROBE_JS).get("authorized"))

    def ensure_jsqr(self):
        # SPA navigations recreate the JS context and drop injected globals.
        if self.browser.evaluate("typeof jsQR") == "undefined":
            self.browser.evaluate(JSQR_SRC)

    def login_tick(self):
        """One needs-login poll: submit a pending 2FA password, or mirror the
        current QR to /qr.png and (on token change) to the log."""
        if self.browser.evaluate(PASSWORD_PRESENT_JS):
            with STATE.lock:
                STATE.password_needed = True
            password = STATE.take_password()
            if password:
                log("Submitting 2FA password")
                self.browser.evaluate(
                    SUBMIT_PASSWORD_JS.replace(
                        "__PASSWORD__", json.dumps(password)))
                time.sleep(2)
            return
        with STATE.lock:
            STATE.password_needed = False
        self.ensure_jsqr()
        result = self.browser.evaluate(QR_MIRROR_JS)
        if not result.get("data"):
            return
        png = base64.b64decode(result["png"].split(",", 1)[1])
        if STATE.set_qr(result["data"], png):
            self.print_login_qr(result["data"])

    def print_login_qr(self, token):
        try:
            qr = subprocess.run(
                ["qrencode", "-t", "UTF8"], input=token.encode(),
                capture_output=True, check=True).stdout.decode()
        except (OSError, subprocess.CalledProcessError) as exc:
            log(f"qrencode failed: {exc}")
            qr = f"(QR render failed - open the link manually)\n{token}"
        login_url = (f"http://{CFG['public_host']}:{CFG['public_http_port']}"
                     "/login")
        print(
            "\n==== TELEGRAM LOGIN REQUIRED ====\n"
            "Scan with the Telegram app: Settings -> Devices -> "
            "Link Desktop Device\n"
            f"(or open {login_url} in a browser)\n\n"
            f"{qr}\n"
            "The code rotates every ~30s; a fresh one is printed on each "
            "rotation.\n",
            flush=True)

    # -- detection (narrow interface, swappable in Phase 2) ------------------

    def probe_live(self):
        """is_live() -> (bool, title)."""
        result = self.browser.evaluate(PROBE_JS)
        return bool(result["live"]), (result["title"] or "").strip()

    # -- join ---------------------------------------------------------------

    def rejoin_page(self):
        # A reload auto-rejoins the previous (possibly stuck) call and K
        # never auto-opens the player for a rejoined call. Only a fresh
        # browser boot reliably resets to the pre-join Join bar.
        self.browser.close()
        self.browser.launch(self.channel_url())
        time.sleep(8)

    def join(self):
        """Click into the live stream and stage its <video>. True on success."""
        try:
            # Shed any stuck call from a previous attempt: joins re-attached
            # on boot never open the player. Leave, wait for the Join bar,
            # then join fresh.
            if self.browser.evaluate(LEAVE_STUCK_CALL_JS) == "left":
                log("Left a stuck call from a previous attempt")
                time.sleep(4)
                self.probe_live()  # re-mark the (hopefully) fresh live bar
            self.browser.evaluate(CLICK_LIVE_BAR_JS)
        except BrowserError as exc:
            STATE.set(error=f"join click failed: {exc}")
            return False

        start = time.monotonic()
        deadline = start + CFG["join_timeout"]
        opened = False
        while time.monotonic() < deadline:
            try:
                if self.browser.evaluate(STAGE_VIDEO_JS):
                    self.last_clock = None
                    self.last_video_ok = time.monotonic()
                    return True
                # A confirm dialog ("Join"/"Watch") may pop; press it if seen.
                self.browser.evaluate(CLICK_JOIN_JS)
                # RTMP streams: after joining, the stream player normally
                # auto-opens within seconds. If it hasn't, click the call bar
                # open ONCE - the click toggles the player, so repeating it
                # closes a player that is still loading. On failure the page
                # is reloaded anyway, which resets this state machine.
                if not opened and time.monotonic() - start > 15:
                    self.browser.evaluate(OPEN_PLAYER_JS)
                    opened = True
            except BrowserError as exc:
                STATE.set(error=f"join evaluate failed: {exc}")
                return False
            time.sleep(1)
        STATE.set(error="no playing <video> before JOIN_TIMEOUT")
        return False

    def video_advancing(self):
        """True while the staged video's clock moves forward."""
        clock = self.browser.evaluate(VIDEO_CLOCK_JS)
        if clock is None or clock["ended"]:
            return False
        advancing = self.last_clock is None or clock["t"] > self.last_clock
        self.last_clock = clock["t"]
        if clock["paused"]:
            self.browser.evaluate(STAGE_VIDEO_JS)  # kick playback
        return advancing

    # -- state machine ------------------------------------------------------

    def end_stream(self):
        end_ts = time.time()
        title = self.live_title or "Stream"
        start_ts = self.live_start
        try:
            self.browser.evaluate(UNSTAGE_JS)
        except BrowserError:
            pass
        self.ffmpeg.ensure("slate")
        if CFG["record"] and start_ts is not None:
            threading.Thread(
                target=harvest, args=(title, start_ts, end_ts),
                daemon=True).start()
        self.live_start = None
        STATE.set(state="idle", title="")

    def tick(self):
        state = STATE.snapshot()["state"]

        if state in ("starting", "needs-login"):
            self.ffmpeg.ensure("slate")
            if self.check_authorized():
                if state == "needs-login":
                    log("Login complete - session saved in /state/profile")
                    STATE.clear_login()
                    # The SPA ignores runtime hash navigation (and rewrites
                    # the hash back), so a fresh boot with the channel URL is
                    # the only reliable way to open the chat.
                    self.browser.close()
                    self.browser.launch(self.channel_url())
                    time.sleep(5)
                STATE.set(state="idle")
                return
            if state == "starting":
                STATE.set(state="needs-login")
                log("No Telegram session - waiting for QR login")
            self.login_tick()
            return

        if state in ("idle", "join-failed"):
            self.ffmpeg.ensure("slate")
            # A session revoked from the phone looks like the auth screen.
            if not self.check_authorized():
                log("Telegram session lost - returning to QR login")
                STATE.set(state="needs-login")
                return
            live, title = self.probe_live()
            if not live:
                self.join_failures = 0
                if state == "join-failed":
                    STATE.set(state="idle")
                return
            # Back off after repeated failures so a broken join selector
            # doesn't hammer the page every poll.
            if state == "join-failed" and self.join_failures >= 3 \
                    and time.monotonic() - self.last_join_attempt < 60:
                return
            STATE.set(state="joining", title=title)
            self.last_join_attempt = time.monotonic()
            if self.join():
                self.live_start = time.time()
                self.live_title = title or CFG["name"]
                self.join_failures = 0
                self.ffmpeg.ensure("live")
                STATE.set(state="live", title=self.live_title)
            else:
                self.join_failures += 1
                self.rejoin_page()
                STATE.set(state="join-failed")
            return

        if state == "live":
            self.ffmpeg.ensure("live")
            now = time.monotonic()
            # Re-stage every tick: idempotent, and upgrades to the player's
            # main video if a thumbnail was staged before it finished loading.
            try:
                self.browser.evaluate(STAGE_VIDEO_JS)
            except BrowserError:
                pass
            if self.video_advancing():
                self.last_video_ok = now
                return
            # Stalled. If the live bar is still up, try to re-stage/rejoin;
            # if it is gone past the grace window, the stream has ended.
            live, _ = self.probe_live()
            if now - self.last_video_ok > CFG["end_grace"]:
                log("Video gone past END_GRACE - ending stream")
                self.end_stream()
            elif live and now - self.last_video_ok > 10:
                log("Video stalled while still live - rejoining")
                self.rejoin_page()
                self.probe_live()  # re-mark the bar for join()
                self.join()

    def run(self):
        while True:
            try:
                STATE.set(state="starting")
                self.browser = Browser()
                self.browser.launch(self.channel_url())
                time.sleep(5)  # let Telegram Web boot
                while True:
                    self.tick()
                    # Poll fast while mirroring the login QR (it rotates
                    # every ~30s); normal cadence otherwise.
                    if STATE.snapshot()["state"] == "needs-login":
                        time.sleep(1)
                    else:
                        time.sleep(CFG["poll_interval"])
            except BrowserError as exc:
                STATE.set(error=f"browser failure: {exc}")
            except Exception as exc:  # noqa: BLE001
                STATE.set(error=f"unexpected: {exc}")
            # A dying browser mid-live still leaves segments on disk; harvest
            # what we have before relaunching.
            if self.live_start is not None and CFG["record"]:
                threading.Thread(
                    target=harvest,
                    args=(self.live_title or "Stream", self.live_start, time.time()),
                    daemon=True).start()
                self.live_start = None
            if self.browser is not None:
                self.browser.close()
                self.browser = None
            self.ffmpeg.ensure("slate")
            log("Relaunching browser in 10s")
            time.sleep(10)


def main():
    server = ThreadingHTTPServer(("0.0.0.0", CFG["http_port"]), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log(f"HTTP endpoints on :{CFG['http_port']}, stream at {STREAM_URL}")
    Capture().run()


if __name__ == "__main__":
    main()
