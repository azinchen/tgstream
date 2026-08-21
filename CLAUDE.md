# tgstream

Watch Telegram channels for live streams (group calls), restream them to Plex
and Jellyfin as live TV, and save each stream to a file for offline viewing.

**Status: implemented and verified against real live streams — MTProto
capture, no browser.** This document is the design record; it exists so the
decisions below are not re-litigated. Where it says "decided", treat it as
settled unless the implementation turns up a fact that contradicts the stated
rationale.

**MAJOR ARCHITECTURE CHANGE (2026-08): browser capture replaced by direct
MTProto.** The original design captured video by screen-grabbing Chromium on
Telegram Web (see the superseded §3.1). Testing against real streams proved
that path delivers ~5fps, DVR replays instead of the live edge, A/V desync,
and re-encode quality loss. It was replaced by pulling the RTMP broadcast's
media chunks directly over MTProto and remuxing them with `-c copy`. This
eliminated Chromium, Xvfb, PulseAudio, x11vnc, VAAPI, **and MediaMTX**, cut
the image from ~930MB to ~230MB and CPU from ~2 cores to ~0.1, and gave full
source quality, true live edge, and structural A/V sync. See §3.1.

Implementation conventions: **Alpine** base with **pinned apk versions**,
s6-overlay v3 supervised, following the layout and style of `~/projects/
nordvpn`. Mostly apk, no pip — **one deliberate exception**: `av` (PyAV) is
`pip install`ed (abi3 musllinux wheel, no compile) because the ffmpeg-CLI
`-c copy` remux left VLC and Plex with no audio, while PyAV's in-process
packet remux produces player-clean MPEG-TS. Everything else is apk:
`py3-telethon`, `ffmpeg` (slate/harvest), `libqrencode-tools`. The
audio-duration used for the gapless timeline is still a ~30-line pure-Python
MP4 box scanner in `capture.py` (advance the offset by the chunk's real
~1003ms audio, not a fixed 1000ms grid). Guide is pure shell (`curl` + `jq` +
busybox httpd). No Claude/Anthropic attribution in commits or PRs.

---

## 1. Requirements

Fixed by the user:

- Source is a **closed/private Telegram channel** that streams intermittently.
- Playback targets are **Plex and Jellyfin only**. Chromecast was explicitly
  dropped. This relaxes the encode profile — H.264 High + AAC is fine, no
  baseline constraint.
- Live restreaming **and** recording to file. Recordings are plain files in a
  folder that both servers index as a normal library; no DVR integration needed.
- The channel list must be **configurable**.
- **One container per channel.** Capturing several channels means running
  several containers. This is a user directive, not an inferred preference.

Environment: Docker on a host with a `reverseproxynetwork` external network and
a ZFS pool at `/tank`. User has Plex Pass. Existing appdata convention is
`/tank/appdata/<service>`, media at `/tank/media/<library>`.

---

## 2. Architecture

```
tg-<slug>  ── MTProto: join call, pull 1s chunks ── ffmpeg -c copy ──┐
tg-<slug>  ── MTProto: join call, pull 1s chunks ── ffmpeg -c copy ──┤ HLS + MPEG-TS
                              ┌──────────────────────────────────────┤       │
                              │                                       ▼       ▼
                          guide (M3U + XMLTV + HDHomeRun)        harvest ──► /library
                              ▼
                     Plex / Jellyfin Live TV
```

Components:

| Name | Count | Base image | Role |
|---|---|---|---|
| `tgstream` (capture) | N | `alpine` (~230MB) | one channel: MTProto detect+join+pull, remux, serve HLS/TS, harvest |
| `tgstream-guide` | 1 | `alpine` (~20MB) | merge per-channel `/status` into one M3U + XMLTV + HDHomeRun tuner |

**No MediaMTX.** Each capture container serves its own live HLS
(`/stream.m3u8`), continuous MPEG-TS (`/stream.ts`, for Plex's HDHomeRun
client), and rolling segments on its HTTP port; it records by archiving those
segments and concatenating on stream end. MediaMTX's roles (RTMP-in→HLS,
recording, slate) all moved into the capture container.

**Two published images** (user directive, for public GitHub + GHCR
publishing): `ghcr.io/azinchen/tgstream` is the capture container (with
integrated QR login — no separate login mode; `MODE` no longer exists), while
the guide is its own ~20MB image `ghcr.io/azinchen/tgstream-guide` built from
`guide/` (shell + busybox httpd; a Chromium image serving two text files is
wrong for a public registry). GHCR packages link to the repo via the
`org.opencontainers.image.source` label. **Production releases (v* tags) also
push both images to Docker Hub** (`azinchen/tgstream`, `azinchen/tgstream-guide`)
and upload the root `README.md` as the repo description of both (user
directive: one shared README); dev builds stay GHCR-only. Docker Hub
secrets: `DOCKERHUB_USERNAME`/`DOCKERHUB_TOKEN` (login, cleanup) and
`DOCKERHUB_PASSWORD` (description upload).

---

## 3. Decisions and rationale

### 3.1 Capture over MTProto, not browser — DECIDED (was: browser)

Original decision was browser capture (screen-grab Chromium on Telegram Web),
on the belief that pytgcalls-family libraries only push media *into* calls and
can't pull a broadcast out. That belief was too broad. **Telegram RTMP
broadcasts** (what news/streamer channels use — OBS pushing RTMP) are
server-transcoded and delivered to clients as ~1-second standalone-MP4 chunks
over plain MTProto file requests. A normal Telethon client can pull them:

1. `channels.getFullChannel` → `full_chat.call` (the `InputGroupCall`); None
   ⇒ idle, present ⇒ live. This is also the detector (§3.2).
2. `phone.joinGroupCall` with a **minimal presence payload** — empty
   fingerprints + an arbitrary ssrc, no WebRTC/DTLS handshake. This is the one
   non-obvious requirement: `getFile` returns `GROUPCALL_JOIN_MISSING` until
   you've joined, but joining needs no native tgcalls/ntgcalls layer.
3. `phone.getGroupCallStreamChannels` → `last_timestamp_ms`, `scale`
   (segment_ms = `1000 >> scale`, in practice scale 0 = 1000ms). Live position
   = round down, minus a small buffer.
4. `upload.getFile(InputGroupCallStream(call, time_ms, scale, video_channel=1,
   video_quality))` → one chunk = 32-byte Telegram header + a standalone MP4
   (ftyp/mdat/moov) with H.264 + AAC muxed. `video_channel=1` is the muxed A/V
   track (0 is audio-only, 2 is a low-rate preview at scale −5).
   `TIME_TOO_BIG` = caught up to live; `TIME_TOO_SMALL` = expired, skip;
   `TIME_INVALID` = the call restarted, rejoin.

Each chunk is remuxed `-c copy` to an MPEG-TS HLS segment. Verified against
real streams: full 720p25 H.264 + AAC, true live edge, structural A/V sync,
~0.1 CPU. Measured facts: source ≈ 1.04× realtime, fetch ≈ 3 chunks/s, so a
**producer/consumer pipeline** (fetch decoupled from remux) keeps it ahead of
realtime; a plain fetch-then-remux loop summed the latencies and fell behind.

`noforwards` does **not** block this — clients must fetch these chunks to play
at all, so a member account can too (verified live). The account must be a
member of the channel.

**Known limitation:** phone-camera group *video chats* are WebRTC, not RTMP
broadcasts — those are not capturable this way (nor by Telegram Web K, which
renders them audio-only). Virtually all "live stream" channels use RTMP.

### 3.2 Live detection — DECIDED (MTProto, free)

Detection is `channels.getFullChannel` → `full_chat.call is None`. No browser,
no selectors, no separate mechanism — it falls out of the capture path. The
old §3.2 open question (DOM vs Telethon, and the `AUTH_KEY_DUPLICATED` worry
about sharing one session across N containers) is moot: each capture container
owns its own MTProto session in `/state` (one QR scan per channel), exactly as
each was already its own browser session.

> Correction carried forward from planning: an earlier version of this design
> claimed container-per-channel *causes* the 400MB-per-channel RAM cost. It
> does not. Every channel needs its own Xvfb display, Chromium, and PulseAudio
> sink regardless of packaging — you cannot x11grab two channels off one
> display. The RAM cost belongs to DOM detection, because it forces the browser
> to stay resident while idle. Do not use "we split the containers" as a reason
> to accept it.

### 3.3 Slate loop on idle — DECIDED

Each container is the sole publisher on its MediaMTX path. A path with no
publisher 404s, and Plex throws a tuner error. So when idle, the container
stream-copies a pre-encoded 10s slate loop (`-c copy`, ~0% CPU) to keep the
path alive; on live it swaps to the real capture.

Consequence: switching modes restarts ffmpeg, so there is a ~2s gap at stream
start. Accepted.

The slate must be encoded with the **same codecs and sample rate** as the live
capture (H.264 High / AAC 48kHz stereo) or the `-c copy` publish will not be
compatible with what follows it on the same path.

### 3.4 Recording format: MPEG-TS, not fMP4 — DECIDED

MediaMTX records continuously in 10-minute segments with 24h expiry. On stream
end the container concatenates the segments overlapping its live window with
`-c copy` into the library.

TS specifically because **a segment truncated by an unclean kill still
concatenates**; fMP4 does not. Continuous recording (including slate periods)
is deliberate — it avoids runtime record-toggling via the MediaMTX API, and
slate segments are tiny and self-expiring. Select segments by parsing the
timestamp out of the filename, not by mtime.

### 3.5 No Threadfin / xTeVe — DECIDED (rationale corrected)

Original rationale ("both Plex and Jellyfin ingest M3U directly") was
half-wrong: **Jellyfin does, Plex does not** — M3U tuners remain an open
Plex feature request, and PMS's grabber list has no M3U entry (verified
against PMS 1.43.3). Instead of reintroducing Threadfin, the guide
**emulates an HDHomeRun tuner** (`/discover.json`, `/lineup_status.json`,
`/lineup.json` generated by the same jq refresh loop; `GUIDE_URL` env is
baked into discover.json as BaseURL). Plex is pointed at the guide's
address in the HDHomeRun field; Jellyfin keeps using the M3U. Threadfin
still stays out — it only earns its keep filtering thousand-channel lists.

Confirmed on first tune: Plex's HDHomeRun client requires raw MPEG-TS from
lineup URLs and refuses HLS ("Could not tune channel"). Resolved: the guide
ships ffmpeg and a `/cgi-bin/stream?path=tg-<slug>` CGI that copy-remuxes
the channel's RTMP feed from MediaMTX to MPEG-TS on the fly (no re-encode);
lineup.json points there, while playlist.m3u keeps HLS URLs for
Jellyfin/VLC. `STREAM_SOURCE_BASE` (default `rtmp://tgstream-mediamtx:1935`)
names the RTMP source.

### 3.6 Guide aggregation — DECIDED

Each capture container knows only its own channel and serves a one-entry
`/playlist.m3u`. Plex and Jellyfin each want a single tuner URL, so the guide
container walks the capture containers' `/status` endpoints and merges.

It must fetch upstreams **in parallel and skip unreachable ones**. A blocking
or fatal fetch means one dead capture container errors every tuner in Plex.

### 3.7 Config is environment variables — DECIDED

No `channels.yml`. One channel per container, configured entirely by env, with
a `x-capture` YAML anchor in compose so adding a channel is a five-line block
plus one entry in the guide's `UPSTREAMS`.

### 3.8 Browser sessions — SUPERSEDED (was: shared master profile)

Originally: one interactive VNC login to a master profile on a shared volume,
cloned per container. Superseded (user directive: no VNC, and cloning one
auth key into N concurrently-connected browsers risks `AUTH_KEY_DUPLICATED`
revocation) by **integrated QR login**: each capture container owns its
profile in `/state`, and when it has no session it mirrors Telegram Web's
login QR to its log (vendored jsQR decodes the canvas, `qrencode` re-renders
it as UTF8 blocks) and to `GET /login`, then proceeds automatically once
scanned. One scan per channel, ever; each channel is its own revocable device.

---

## 4. Staging

**Phase 1 — now, N=1.** The user has one channel. Build N containers + guide,
DOM detection, detection behind `is_live(slug) -> (bool, title)`.

At N=1 the guide container is arguably overhead, but build it anyway: it is
small, and retrofitting aggregation after Plex is already pointed at a
single-channel endpoint means re-pairing tuners.

**Phase 2 — at roughly channel three.** Factor detection into a
`tgstream-detect` sidecar: one container, one Telethon session, watches every
configured channel, serves live state. It already knows every channel's name,
title, and live status, so it **absorbs the guide** — the separate guide
container disappears.

This is strictly better than Phase 1: single session, idle channels at ~30MB
instead of 400MB, per-channel isolation retained, guide aggregation free. It is
staged only because at N=1 it is pure overhead.

Phase 2's cost is a hard dependency — detector down means nothing captures. Do
not build a DOM-detection fallback path in the capture container to mitigate
this; it reintroduces the brittle selectors as a second code path that will rot
untested. `restart: unless-stopped` plus alerting is enough.

Phase 2 also introduces a real tradeoff to decide *then*, not now: cold-starting
Chromium at stream start costs the first **15–25 seconds** of the broadcast
(launch, load Telegram Web, navigate, join). A `KEEP_BROWSER_WARM` flag cuts it
to ~5s at 400MB standing. For an intermittent channel, cold start is the right
default.

---

## 5. Interfaces

### 5.1 Capture container environment

| Variable | Default | Meaning |
|---|---|---|
| `PUID` / `PGID` | `0` / `0` | run capture as this uid:gid; recordings owned by it (0 = root) |
| `TG_SLUG` | *required* | `[a-z0-9-]`; stream path is `tg-<slug>` |
| `TG_PEER` | *required* | channel id, `@username`, or `t.me/+invite` URL |
| `API_ID` / `API_HASH` | *required* | Telegram app creds (my.telegram.org) |
| `TG_NAME` | slug | display name in the guide |
| `TG_CHANNEL_NUMBER` | `1` | guide ordering |
| `RECORD` | `true` | write finished streams to `/library` |
| `VIDEO_QUALITY` | `2` | Telegram stream quality tier for `getFile` |
| `POLL_INTERVAL` | `5` | seconds between liveness checks |
| `BUFFER_MS` | `2000` | how far behind live to start (latency vs stability) |
| `HLS_WINDOW` / `HLS_GRACE` | `12` / `8` | playlist window / extra segments kept (404 guard) |
| `PUBLIC_HOST` | `localhost` | must resolve from Plex, Jellyfin **and** clients |
| `HTTP_PORT` | `8409` | in-container port |
| `PUBLIC_HTTP_PORT` | `HTTP_PORT` | host-published port (for the `/login` URL) |

No encode settings: capture is `-c copy` at the source quality/resolution, so
there is no `CAPTURE_*`, `VAAPI`, `RTMP_URL`, `shm_size`, or `/dev/dri`.
`TG_SLUG` becomes the library folder name — changing it orphans recordings.

### 5.2 HTTP endpoints

Capture container, on `HTTP_PORT` (all media + control on one port):

- `GET /stream.m3u8` — live HLS (slate loop when idle)
- `GET /stream.ts` — never-ending MPEG-TS (Plex's HDHomeRun client)
- `GET /s<N>.ts` — HLS segments
- `GET /playlist.m3u`, `GET /epg.xml` — one-channel M3U / XMLTV
- `GET /status` — JSON: `slug`, `name`, `path`, `channel_number`, `state`,
  `title`, `since`, `since_ts`, `url`, `record`, `last_error`
- `GET /login`, `GET /qr.png`, `POST /login/password` — integrated QR login

`state` is one of `starting | needs-login | idle | live`. In `needs-login` the
container prints the QR to the log (`qrencode -t UTF8`) on each ~30s token
rotation; the healthcheck goes unhealthy after 10 minutes of waiting.

Guide serves merged `/status`, `/playlist.m3u`, `/epg.xml`, and the HDHomeRun
endpoints (`/discover.json`, `/lineup.json`, `/lineup_status.json`) whose
lineup URLs point at each capture's `/stream.ts`. `UPSTREAMS` is a
comma-separated list of capture base URLs.

### 5.3 Volumes

| Path | Mode | Scope |
|---|---|---|
| `/state` | rw | **per channel** — MTProto session (`session.session`), slate |
| `/library` | rw | shared; finished recordings |

Live segments live in `/run/tgstream` (tmpfs), rotated automatically — no
shared segment volume. Each capture container owns its MTProto session in
`/state` (one QR scan per channel).

### 5.4 Recording output

```
/tank/media/telegram/<TG_NAME> - YYYY-MM-DD HH-MM - <Stream Title>.mp4
```

Added to Plex and Jellyfin as a **TV Shows** library, date-based episode
ordering in Plex. `HH-MM` is the stream start time — 24/7 channels roll over
several times a day, so date alone collides and the files sort by name.
Sanitize the stream title for filesystem-hostile characters, drop it entirely
when it merely repeats `TG_NAME` (common on 24/7 streams), and collide-suffix
with ` (2)`.

---

## 6. Known-fragile areas

**Telegram Web K selectors.** The live-stream topbar is the thing that moves,
and under DOM detection it carries both detection and joining. Keep the
selector list and a visible-text fallback probe in one clearly marked block.
Include Russian strings (`Прямой эфир`, `Трансляция`, `Голосовой чат`,
`Видеочат`) — the user's interface is likely Russian. The QR login adds three
more fragile points to the same block: the `user_auth` localStorage key (auth
probe), the square-canvas heuristic for the QR (the biggest canvas is the
doodle wallpaper; the QR canvas is transparent and must be composited onto
white before jsQR decodes it), and the `input[type=password]` 2FA field.

Video handling should be **selector-free**: find the largest playing `<video>`,
reparent it into a full-viewport stage, hide everything else. This survives most
UI churn, so a rename costs one line in the selector list rather than a rewrite.

**Silent failure mode.** Under DOM detection, a selector break looks identical
to "channel is idle" — slate forever, no error. Worth a healthcheck that flags
a channel reporting `idle` beyond an expected window.

**Watchdogs needed at three levels:** a stalled `<video>` triggers rejoin; a
dead ffmpeg is restarted in its current mode; a browser that stops responding
to `evaluate` is relaunched.

---

## 7. Performance expectations

- **CPU**: ~1.5–2 cores per 1080p30 stream on `libx264 -preset veryfast`.
  VAAPI (`/dev/dri` passthrough, `h264_vaapi`) drops it to ~0.3.
- **RAM**: ~400MB idle per container under DOM detection, ~700MB capturing.
- **Latency**: 8–15s end to end with 2s HLS segments.
- **Fewer pixels beats a faster preset.** Capturing a 1080p source at 720p
  roughly halves CPU and no phone client will notice.

---

## 8. Repository layout (as implemented)

- `Dockerfile` — s6-builder → rootfs-builder → `alpine:3.24` main stage with
  pinned apk packages: ffmpeg, py3-telethon (+ py3-pyaes, py3-rsa),
  libqrencode-tools, fonts, jq, curl, python3. No browser.
- `root/usr/local/bin/` — `entrypoint` (banner → `/init`),
  `backend-functions`, `make-slate`, `tgstream-healthcheck`.
- `root/etc/s6-overlay/s6-rc.d/` — `init-tgstream` (validate env, slate),
  `svc-capture`; static `user/contents.d`.
- `root/opt/tgstream/capture.py` — Telethon MTProto client, QR login,
  detection, download pipeline (producer/consumer), ffmpeg-CLI remux, HLS +
  MPEG-TS serving, slate loop, recorder/harvest. Includes the pure-Python MP4
  audio-duration parser (gapless timeline: advance offset by the chunk's real
  ~1003ms audio duration, not a fixed 1000ms grid, or the AAC decoder drops a
  frame per second — the audio-drop bug).
- `guide/` — the separate guide image: own `Dockerfile` and `root/` with
  `svc-guide` (shell refresh loop writing /run/guide, incl. HDHomeRun JSON)
  and `svc-guide-httpd` (busybox httpd), static s6 user bundle.
- `docker-compose.yml` (`x-capture` anchor), `.env.example`, `README.md`.

Host paths are **not hardcoded**: `.env` defines `APPDATA_DIR` (per-channel
state) and `LIBRARY_DIR` (recordings); `/tank/appdata/tgstream` and
`/tank/media/telegram` are only the example defaults.

---

## 9. Verifying

- **Slate/idle**: bring up a container with no live stream; `curl
  http://<host>:<port>/stream.m3u8` returns the slate HLS.
- **Detection + capture**: point at a channel that is actually live (RTMP
  broadcast, member account); state goes `idle → live`, `/stream.m3u8` and
  `/stream.ts` serve real video within seconds.
- **Recording**: on stream end the segments concat to `/library/<Name>/…mp4`.
- **Guide**: stub the upstreams with a static JSON server; verify a dead
  upstream degrades to a shorter playlist rather than a 500.
- **Plex/Jellyfin wiring**: pair the tuner against the slate-only stream first.
  If the slate tunes and plays, the live stream will.

---

## 10. Open questions for the user

1. Confirm host paths — `/tank/appdata/tgstream/{session,segments,state}` and
   `/tank/media/telegram` are assumed.
2. `PUBLIC_HOST` value.
3. Does the host CPU have a usable iGPU? (`vainfo`) Decides `CAPTURE_ENCODER`.
4. Expected concurrent stream count — currently assumed 1, occasionally 2.
5. Retention for recordings; nothing prunes `/library` today.
