#!/usr/bin/env python3
"""tgstream capture: pull one Telegram channel's live stream directly over
MTProto (no browser) and serve it as HLS + MPEG-TS for Plex/Jellyfin, while
recording each stream to the library.

Telegram RTMP broadcasts are delivered as ~1s standalone-MP4 chunks over
MTProto (join the call for presence, then upload.getFile the chunks). Each
chunk is remuxed (-c copy, no re-encode) to an MPEG-TS HLS segment on a
gapless timeline. Detection, join, download and recording are all API calls -
no Chromium, no Xvfb, no MediaMTX.
"""

import asyncio
import datetime
import html
import json
import os
import re
import io
import struct
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import av
from telethon import TelegramClient, functions, types
from telethon.errors import RPCError, SessionPasswordNeededError


def env(name, default=None, required=False):
    v = os.environ.get(name, "")
    if not v:
        if required:
            raise SystemExit(f"CRITICAL ERROR: {name} is required")
        return default
    return v


CFG = {
    "slug": env("TG_SLUG", required=True),
    "peer": env("TG_PEER", required=True),
    "name": env("TG_NAME") or env("TG_SLUG", required=True),
    "channel_number": int(env("TG_CHANNEL_NUMBER", "1")),
    "api_id": int(env("API_ID", required=True)),
    "api_hash": env("API_HASH", required=True),
    "record": env("RECORD", "true").lower() == "true",
    "public_host": env("PUBLIC_HOST", "localhost"),
    "http_port": int(env("HTTP_PORT", "8409")),
    "public_http_port": int(env("PUBLIC_HTTP_PORT", env("HTTP_PORT", "8409"))),
    "poll_interval": float(env("POLL_INTERVAL", "5")),
    "buffer_ms": int(env("BUFFER_MS", "2000")),
    "window": int(env("HLS_WINDOW", "16")),
    "grace": int(env("HLS_GRACE", "40")),
    "video_quality": int(env("VIDEO_QUALITY", "2")),
    "state_dir": env("STATE_DIR", "/state"),
    "library_dir": env("LIBRARY_DIR", "/library"),
    "run_dir": env("RUN_DIR", "/run/tgstream"),
}

PATH = f"tg-{CFG['slug']}"
HLS_DIR = os.path.join(CFG["run_dir"], "hls")
REC_DIR = os.path.join(CFG["run_dir"], "rec")
STREAM_URL = (f"http://{CFG['public_host']}:{CFG['public_http_port']}"
              f"/stream.m3u8")
XMLTV_FMT = "%Y%m%d%H%M%S %z"
JOIN_SSRC = 0x50000000


def log(msg):
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{stamp} [CAPTURE] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Minimal MP4 parser: audio-track content duration (seconds).
#
# Telegram chunks carry ~1003ms of audio (47 AAC frames) but 1000ms of video.
# Advancing the output timeline by the AUDIO duration keeps audio perfectly
# gapless and A/V synced (shared offset), leaving only an imperceptible ~3ms
# video micro-gap. The audio track's mdhd has timescale 48000.
# ---------------------------------------------------------------------------

def audio_duration(mp4: bytes) -> float:
    best = None
    i = 0
    n = len(mp4)
    # Scan for every 'mdhd' box and read its timescale/duration; the audio
    # track is the one with timescale == the audio sample rate (48000).
    while True:
        j = mp4.find(b"mdhd", i)
        if j < 0:
            break
        i = j + 4
        try:
            ver = mp4[j + 4]
            if ver == 1:
                ts = struct.unpack(">I", mp4[j + 4 + 20:j + 4 + 24])[0]
                dur = struct.unpack(">Q", mp4[j + 4 + 24:j + 4 + 32])[0]
            else:
                ts = struct.unpack(">I", mp4[j + 4 + 12:j + 4 + 16])[0]
                dur = struct.unpack(">I", mp4[j + 4 + 16:j + 4 + 20])[0]
            if ts in (48000, 44100, 24000, 16000) and dur:
                best = dur / ts
        except (struct.error, IndexError):
            continue
    return best if best else 1.0


def sanitize_title(title):
    title = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", title).strip().rstrip(".")
    return title[:120] or "Stream"


# ---------------------------------------------------------------------------
# Shared state (HTTP reads it, the capture loop writes it)
# ---------------------------------------------------------------------------

class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.state = "starting"     # starting|needs-login|idle|live
        self.title = None
        self.since = None
        self.last_error = None
        self.qr_png = None
        self.qr_token = None
        self.password_needed = False
        self.pending_password = None

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
                "slug": CFG["slug"], "name": CFG["name"], "path": PATH,
                "channel_number": CFG["channel_number"], "state": self.state,
                "title": self.title,
                "since": self.since.isoformat() if self.since else None,
                "since_ts": self.since.timestamp() if self.since else None,
                "url": STREAM_URL, "record": CFG["record"],
                "last_error": self.last_error,
            }


STATE = State()


# ---------------------------------------------------------------------------
# HLS: rolling playlist of MPEG-TS segments written by the streamer.
# ---------------------------------------------------------------------------

class Hls:
    """Owns the segment directory and the live m3u8. Segments are written by
    remux(); the playlist advertises a rolling WINDOW, files are kept GRACE
    longer so a lagging player never 404s."""

    def __init__(self):
        self.lock = threading.Lock()
        self.idx = 0
        self.seq = 0
        self.segs = []          # (index, duration) in the playlist window
        self.off = 0.0          # running timeline offset (seconds)
        self.disc_pending = False
        os.makedirs(HLS_DIR, exist_ok=True)

    def reset_offset(self):
        # Called on (re)join / slate<->live transitions: next segment carries
        # an EXT-X-DISCONTINUITY so players re-baseline their clock.
        self.disc_pending = True

    def add(self, mp4_bytes, record_writer=None):
        """Remux one chunk to a segment, publish it, return its duration."""
        idx = self.idx
        out_ts = os.path.join(HLS_DIR, f"s{idx}.ts")
        part = out_ts + ".part"
        dur = audio_duration(mp4_bytes)
        # In-process packet remux (PyAV): copy the already-encoded H.264/AAC
        # packets onto the running gapless timeline. This produces
        # player-clean MPEG-TS - the ffmpeg-CLI -c copy path left VLC/Plex
        # without audio.
        try:
            inp = av.open(io.BytesIO(mp4_bytes), mode="r")
            out = av.open(part, mode="w", format="mpegts")
            omap = {}
            for s in inp.streams:
                if s.type in ("video", "audio"):
                    try:
                        omap[s.index] = out.add_stream_from_template(s)
                    except AttributeError:
                        omap[s.index] = out.add_stream(template=s)
            for pkt in inp.demux():
                if pkt.dts is None or pkt.stream.index not in omap:
                    continue
                off = int(round(self.off / float(pkt.time_base)))
                pkt.pts = (pkt.pts or 0) + off
                pkt.dts = pkt.dts + off
                pkt.stream = omap[pkt.stream.index]
                out.mux(pkt)
            out.close()
            inp.close()
        except Exception as exc:  # noqa: BLE001
            STATE.set(error=f"remux: {exc}")
            return dur
        try:
            os.replace(part, out_ts)
        except OSError:
            return dur
        if record_writer is not None:
            record_writer(out_ts)
        with self.lock:
            disc = self.disc_pending
            self.disc_pending = False
            self.segs.append((idx, dur, disc))
            if len(self.segs) > CFG["window"]:
                self.segs.pop(0)
                self.seq += 1
            self._write_playlist()
            self.off += dur
            self.idx += 1
        old = os.path.join(HLS_DIR, f"s{idx - CFG['window'] - CFG['grace']}.ts")
        if os.path.exists(old):
            try:
                os.remove(old)
            except OSError:
                pass
        return dur

    def _write_playlist(self):
        target = max((int(d) + 1 for _, d, _ in self.segs), default=1)
        lines = ["#EXTM3U", "#EXT-X-VERSION:3",
                 f"#EXT-X-TARGETDURATION:{target}",
                 f"#EXT-X-MEDIA-SEQUENCE:{self.seq}"]
        for i, d, disc in self.segs:
            if disc:
                lines.append("#EXT-X-DISCONTINUITY")
            lines.append(f"#EXTINF:{d:.3f},")
            lines.append(f"s{i}.ts")
        tmp = os.path.join(HLS_DIR, "index.m3u8.tmp")
        with open(tmp, "w") as f:
            f.write("\n".join(lines) + "\n")
        os.replace(tmp, os.path.join(HLS_DIR, "index.m3u8"))

    def playlist_bytes(self):
        p = os.path.join(HLS_DIR, "index.m3u8")
        try:
            with open(p, "rb") as f:
                return f.read()
        except OSError:
            return None

    def segment_path(self, name):
        if not re.fullmatch(r"s\d+\.ts", name):
            return None
        return os.path.join(HLS_DIR, name)

    def latest_index(self):
        with self.lock:
            return self.idx


HLS = Hls()


# ---------------------------------------------------------------------------
# Recording: archive live segments, concat to mp4 on stream end.
# ---------------------------------------------------------------------------

class Recorder:
    """Append each segment's bytes to one growing MPEG-TS on disk (segments
    share a continuous timeline, so byte-append concatenates cleanly), then
    remux to mp4 on stream end. A single growing file - not 1000s of small
    tmpfs files - so long streams don't pile up."""

    def __init__(self):
        self.active = False
        self.fh = None
        self.tmp_ts = None
        self.title = None
        self.start_ts = None

    def begin(self, title):
        if not CFG["record"]:
            return
        os.makedirs(REC_DIR, exist_ok=True)
        self.title = title or CFG["name"]
        self.start_ts = time.time()
        self.tmp_ts = os.path.join(REC_DIR, "recording.ts")
        try:
            self.fh = open(self.tmp_ts, "wb")
        except OSError as exc:
            STATE.set(error=f"record open failed: {exc}")
            return
        self.active = True
        log(f"Recording started: {self.title}")

    def add(self, ts_path):
        if not self.active or self.fh is None:
            return
        try:
            with open(ts_path, "rb") as s:
                self.fh.write(s.read())
        except OSError as exc:
            STATE.set(error=f"record append failed: {exc}")

    def finish(self):
        if not self.active:
            return
        self.active = False
        try:
            self.fh.close()
        except OSError:
            pass
        self.fh = None
        if not self.tmp_ts or not os.path.exists(self.tmp_ts) \
                or os.path.getsize(self.tmp_ts) == 0:
            return
        threading.Thread(
            target=self._harvest, args=(self.title, self.start_ts, self.tmp_ts),
            daemon=True).start()

    def _harvest(self, title, start_ts, ts_file):
        try:
            date = datetime.datetime.fromtimestamp(start_ts).strftime("%Y-%m-%d")
            out_dir = os.path.join(CFG["library_dir"], CFG["name"])
            os.makedirs(out_dir, exist_ok=True)
            base = f"{CFG['name']} - {date} - {sanitize_title(title)}"
            out = os.path.join(out_dir, f"{base}.mp4")
            n = 2
            while os.path.exists(out):
                out = os.path.join(out_dir, f"{base} ({n}).mp4")
                n += 1
            tmp = out + ".part"
            log(f"Harvesting recording -> {out}")
            subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                 "-i", ts_file, "-c", "copy", "-movflags", "+faststart",
                 "-f", "mp4", tmp],
                check=True)
            os.replace(tmp, out)
            log(f"Harvest complete: {out}")
        except Exception as exc:  # noqa: BLE001
            STATE.set(error=f"harvest failed: {exc}")
        finally:
            try:
                os.remove(ts_file)
            except OSError:
                pass


REC = Recorder()


# ---------------------------------------------------------------------------
# HTTP endpoints
# ---------------------------------------------------------------------------

def render_playlist():
    s = STATE.snapshot()
    return "\n".join([
        "#EXTM3U",
        (f'#EXTINF:-1 tvg-id="{s["slug"]}" tvg-name="{s["name"]}" '
         f'tvg-chno="{s["channel_number"]}" group-title="Telegram",{s["name"]}'),
        s["url"], ""])


def render_epg():
    s = STATE.snapshot()
    now = datetime.datetime.now(datetime.timezone.utc).replace(
        minute=0, second=0, microsecond=0)
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<tv generator-info-name="tgstream">',
           f'  <channel id="{s["slug"]}">',
           f'    <display-name>{html.escape(s["name"])}</display-name>',
           '  </channel>']

    def prog(a, b, title):
        out.append(f'  <programme start="{a.strftime(XMLTV_FMT)}" '
                   f'stop="{b.strftime(XMLTV_FMT)}" channel="{s["slug"]}">')
        out.append(f'    <title>{html.escape(title)}</title>')
        out.append('  </programme>')

    if s["state"] == "live" and s["since"]:
        since = datetime.datetime.fromisoformat(s["since"])
        prog(since, now + datetime.timedelta(hours=4),
             s["title"] or f"{s['name']} live")
    else:
        for i in range(12):
            prog(now + datetime.timedelta(hours=i),
                 now + datetime.timedelta(hours=i + 1),
                 f"{s['name']} (no stream)")
    out.append('</tv>')
    return "\n".join(out)


def render_login_page():
    s = STATE.snapshot()
    if s["state"] != "needs-login":
        return ("<p>✓ Logged in.</p>"
                f"<p>State: <b>{html.escape(s['state'])}</b></p>"), 30
    with STATE.lock:
        pw = STATE.password_needed
        token = STATE.qr_token
    if pw:
        return ("<p>Two-factor password required:</p>"
                '<form method="post" action="/login/password">'
                '<input type="password" name="password" autofocus>'
                ' <button>Submit</button></form>'), 0
    if token:
        return ("<p>Scan with Telegram: <b>Settings &rarr; Devices &rarr; "
                "Link Desktop Device</b></p>"
                '<p><img src="/qr.png" width="300" height="300"'
                ' style="image-rendering:pixelated"></p>'
                f"<p><code>{html.escape(token)}</code></p>"), 3
    return "<p>Waiting for QR…</p>", 2


class Handler(BaseHTTPRequestHandler):
    def _send(self, body, ctype, code=200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/stream.m3u8", "/index.m3u8"):
            pl = HLS.playlist_bytes()
            if pl is None:
                self.send_error(404)
            else:
                self._send(pl, "application/vnd.apple.mpegurl")
            return
        if path == "/stream.ts":
            self._serve_continuous_ts()
            return
        if re.fullmatch(r"/s\d+\.ts", path):
            seg = HLS.segment_path(path.lstrip("/"))
            if seg and os.path.exists(seg):
                with open(seg, "rb") as f:
                    self._send(f.read(), "video/mp2t")
            else:
                self.send_error(404)
            return
        if path == "/qr.png":
            with STATE.lock:
                png = STATE.qr_png
            if png is None:
                self.send_error(404)
            else:
                self._send(png, "image/png")
            return
        if path == "/login":
            body, refresh = render_login_page()
            meta = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
            page = (f"<!doctype html><title>{html.escape(CFG['name'])} login"
                    f"</title>{meta}<body style='font-family:sans-serif;"
                    f"max-width:40em;margin:2em auto'>"
                    f"<h2>tgstream: {html.escape(CFG['name'])}</h2>{body}")
            self._send(page.encode(), "text/html")
            return
        routes = {
            "/status": (lambda: json.dumps(STATE.snapshot()), "application/json"),
            "/playlist.m3u": (render_playlist, "audio/x-mpegurl"),
            "/epg.xml": (render_epg, "application/xml"),
        }
        r = routes.get(path)
        if r is None:
            self.send_error(404)
        else:
            self._send(r[0]().encode(), r[1])

    def do_POST(self):
        if self.path.split("?")[0] != "/login/password":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", 0))
        import urllib.parse
        form = urllib.parse.parse_qs(self.rfile.read(length).decode())
        pw = (form.get("password") or [""])[0]
        if pw:
            with STATE.lock:
                STATE.pending_password = pw
        self.send_response(303)
        self.send_header("Location", "/login")
        self.end_headers()

    def _serve_continuous_ts(self):
        # Never-ending MPEG-TS for Plex's HDHomeRun client: stream segment
        # files back to back as they are produced, starting near live.
        self.send_response(200)
        self.send_header("Content-Type", "video/mp2t")
        self.end_headers()
        nxt = max(0, HLS.latest_index() - 3)
        idle = 0
        while True:
            seg = os.path.join(HLS_DIR, f"s{nxt}.ts")
            if os.path.exists(seg):
                try:
                    with open(seg, "rb") as f:
                        self.wfile.write(f.read())
                except (BrokenPipeError, ConnectionResetError, OSError):
                    return
                nxt += 1
                idle = 0
            else:
                if nxt < HLS.latest_index() - CFG["window"] - CFG["grace"]:
                    nxt = HLS.latest_index()  # fell too far behind
                time.sleep(0.2)
                idle += 1
                if idle > 3000:
                    return

    def log_message(self, *a):
        pass


def start_http():
    srv = ThreadingHTTPServer(("0.0.0.0", CFG["http_port"]), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log(f"HTTP on :{CFG['http_port']} (stream {STREAM_URL})")


# ---------------------------------------------------------------------------
# Slate: publish a static card as HLS when no stream is live, so Plex never
# sees a dead tuner. Generated once by make-slate into /state/slate.ts.
# ---------------------------------------------------------------------------

def slate_loop(stop_evt):
    slate = os.path.join(CFG["state_dir"], "slate.ts")
    if not os.path.exists(slate):
        log("No slate.ts - idle channel will have no filler")
        return
    with open(slate, "rb") as f:
        data = f.read()
    dur = 10.0
    try:
        p = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nk=1:nw=1", slate],
            capture_output=True, text=True)
        dur = float(p.stdout.strip() or 10.0)
    except (ValueError, OSError):
        pass
    HLS.reset_offset()
    while not stop_evt.is_set():
        idx = HLS.idx
        out_ts = os.path.join(HLS_DIR, f"s{idx}.ts")
        part = out_ts + ".part"
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-i", slate, "-c", "copy", "-muxdelay", "0",
             "-output_ts_offset", f"{HLS.off:.3f}", "-f", "mpegts", part],
            check=False)
        try:
            os.replace(part, out_ts)
        except OSError:
            break
        with HLS.lock:
            HLS.segs.append((idx, dur, HLS.disc_pending))
            HLS.disc_pending = False
            if len(HLS.segs) > CFG["window"]:
                HLS.segs.pop(0)
                HLS.seq += 1
            HLS._write_playlist()
            HLS.off += dur
            HLS.idx += 1
        old = os.path.join(HLS_DIR, f"s{idx - CFG['window'] - CFG['grace']}.ts")
        if os.path.exists(old):
            try:
                os.remove(old)
            except OSError:
                pass
        stop_evt.wait(dur * 0.8)


# ---------------------------------------------------------------------------
# MTProto capture
# ---------------------------------------------------------------------------

class Capture:
    def __init__(self):
        self.client = None
        self.entity = None

    async def qr_login(self):
        import base64
        if await self.client.is_user_authorized():
            return
        STATE.set(state="needs-login")
        qr = await self.client.qr_login()
        last_printed = None
        while True:
            STATE.set(title="")
            url = qr.url
            png = self._qr_png(url)
            with STATE.lock:
                STATE.qr_png = png
                STATE.qr_token = url
            if url != last_printed:
                self._print_qr(url)
                last_printed = url
            # 2FA password, if the user submitted one via /login.
            with STATE.lock:
                pw = STATE.pending_password
                STATE.pending_password = None
            try:
                if await qr.wait(timeout=20):
                    break
            except asyncio.TimeoutError:
                pass
            except SessionPasswordNeededError:
                with STATE.lock:
                    STATE.password_needed = True
                if pw:
                    await self.client.sign_in(password=pw)
                    break
            await qr.recreate()
        with STATE.lock:
            STATE.qr_png = None
            STATE.qr_token = None
            STATE.password_needed = False
        log("Login complete - session saved in /state")

    def _qr_png(self, url):
        try:
            p = subprocess.run(["qrencode", "-t", "PNG", "-o", "-", "-s", "6",
                                url], capture_output=True)
            return p.stdout or None
        except OSError:
            return None

    def _print_qr(self, url):
        try:
            qr = subprocess.run(["qrencode", "-t", "UTF8", url],
                                capture_output=True, text=True).stdout
        except OSError:
            qr = url
        login = (f"http://{CFG['public_host']}:{CFG['public_http_port']}/login")
        print(f"\n==== TELEGRAM LOGIN REQUIRED ({CFG['name']}) ====\n"
              f"Scan with Telegram: Settings -> Devices -> Link Desktop Device\n"
              f"(or open {login} )\n\n{qr}\n"
              f"Code rotates ~every 30s.\n", flush=True)

    async def resolve_peer(self):
        peer = CFG["peer"].strip()
        if peer.startswith("@") or "t.me/" in peer:
            return await self.client.get_entity(peer)
        # Numeric channel id. Telegram Web hashes it as "-<channel_id>";
        # bot-API marks it "-100<channel_id>". Populate the entity cache
        # (access_hash for private channels) then resolve.
        await self.client.get_dialogs()
        try:
            n = int(peer)
        except ValueError:
            return await self.client.get_entity(peer)
        for cand in self._id_candidates(n):
            try:
                return await self.client.get_entity(cand)
            except (ValueError, RPCError):
                continue
        raise SystemExit(f"CRITICAL ERROR: cannot resolve TG_PEER '{peer}' - "
                         "is the account a member of the channel?")

    @staticmethod
    def _id_candidates(n):
        s = str(n)
        cands = [n]
        if s.startswith("-100"):
            cands.append(types.PeerChannel(int(s[4:])))
        elif n < 0:
            cid = -n
            cands += [types.PeerChannel(cid), int(f"-100{cid}")]
        else:
            cands += [types.PeerChannel(n), int(f"-100{n}")]
        return cands

    async def live_call(self):
        full = await self.client(
            functions.channels.GetFullChannelRequest(channel=self.entity))
        return full.full_chat.call

    async def join(self, call):
        params = json.dumps({"fingerprints": [], "pwd": "", "ssrc": JOIN_SSRC,
                             "ssrc-groups": [], "ufrag": ""})
        await self.client(functions.phone.JoinGroupCallRequest(
            call=call, join_as=types.InputPeerSelf(), muted=True,
            video_stopped=True, params=types.DataJSON(data=params)))

    async def fetch(self, call, ts, scale):
        loc = types.InputGroupCallStream(
            call=call, time_ms=ts, scale=scale, video_channel=1,
            video_quality=CFG["video_quality"])
        try:
            res = await self.client(functions.upload.GetFileRequest(
                location=loc, offset=0, limit=1024 * 1024))
            return ("ok", res.bytes[32:])
        except RPCError as e:
            s = str(e)
            for k, tag in (("TIME_TOO_BIG", "big"), ("TIME_TOO_SMALL", "small"),
                           ("TIME_INVALID", "rejoin"),
                           ("GROUPCALL_JOIN_MISSING", "rejoin")):
                if k in s:
                    return (tag, None)
            if "JoinMissing" in repr(e):
                return ("rejoin", None)
            return ("err", repr(e))

    async def stream_call(self, call):
        """Download and publish one live call until it ends."""
        ch = await self.client(
            functions.phone.GetGroupCallStreamChannelsRequest(call=call))
        if not ch.channels:
            return
        scale = ch.channels[0].scale
        seg = 1000 >> scale
        t0 = (ch.channels[0].last_timestamp_ms // seg) * seg - CFG["buffer_ms"]

        title = STATE.title
        REC.begin(title)
        STATE.set(state="live")
        HLS.reset_offset()

        queue = asyncio.Queue(maxsize=8)
        loop = asyncio.get_event_loop()

        async def producer():
            t = t0
            big = 0
            while True:
                status, data = await self.fetch(call, t, scale)
                if status == "ok":
                    big = 0
                    await queue.put(data)
                    t += seg
                elif status == "big":
                    await asyncio.sleep(0.2)
                    big += 1
                    if big > 150:
                        if (await self.live_call()) is None:
                            break
                        big = 0
                elif status == "small":
                    t += seg
                elif status == "rejoin":
                    break
                else:
                    log(f"getFile: {data}")
                    await asyncio.sleep(0.5)
            await queue.put(None)

        async def consumer():
            while True:
                data = await queue.get()
                if data is None:
                    break
                await loop.run_in_executor(
                    None, HLS.add, data,
                    REC.add if REC.active else None)

        await asyncio.gather(producer(), consumer())
        REC.finish()

    async def run(self):
        os.makedirs(CFG["state_dir"], exist_ok=True)
        self.client = TelegramClient(
            os.path.join(CFG["state_dir"], "session"),
            CFG["api_id"], CFG["api_hash"])
        await self.client.connect()
        await self.qr_login()
        self.entity = await self.resolve_peer()

        stop_slate = threading.Event()
        slate_thread = None

        def start_slate():
            nonlocal slate_thread
            stop_slate.clear()
            slate_thread = threading.Thread(
                target=slate_loop, args=(stop_slate,), daemon=True)
            slate_thread.start()

        def stop_slate_fn():
            stop_slate.set()

        STATE.set(state="idle")
        start_slate()
        while True:
            try:
                call = await self.live_call()
            except RPCError as e:
                STATE.set(error=f"detect: {e!r}")
                await asyncio.sleep(CFG["poll_interval"])
                continue
            if call is None:
                if STATE.snapshot()["state"] != "idle":
                    STATE.set(state="idle", title="")
                    start_slate()
                await asyncio.sleep(CFG["poll_interval"])
                continue
            # Live: stop slate, join, stream until it ends.
            stop_slate_fn()
            try:
                await self.join(call)
            except RPCError as e:
                STATE.set(error=f"join: {e!r}")
                await asyncio.sleep(3)
                start_slate()
                continue
            try:
                await self.stream_call(call)
            except Exception as e:  # noqa: BLE001
                STATE.set(error=f"stream: {e!r}")
            STATE.set(state="idle", title="")
            start_slate()
            await asyncio.sleep(2)


def main():
    start_http()
    Capture_ = Capture()
    asyncio.run(Capture_.run())


if __name__ == "__main__":
    main()
