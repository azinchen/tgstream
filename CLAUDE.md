# tgstream

Watch Telegram channels for live streams (group calls), restream them to Plex
and Jellyfin as live TV, and save each stream to a file for offline viewing.

**Status: implemented (Phase 1, untested against a real live stream).** This
document is the design record; it exists so the decisions below are not
re-litigated. Where it says "decided", treat it as settled unless the
implementation turns up a fact that contradicts the stated rationale.

Implementation conventions: single image, **Alpine** base with **pinned apk
versions**, s6-overlay v3 supervised, following the layout and style of
`~/projects/nordvpn` (multi-stage Dockerfile with s6-builder/rootfs-builder
stages, `root/` overlay tree, `init-*` oneshots and `svc-*` longruns, shared
`backend-functions`, banner entrypoint that execs `/init`). No
Claude/Anthropic attribution in commits or PRs.

**No Playwright, no pip** (user directive: prefer Alpine and plain scripts).
Playwright is glibc-only and cannot run on musl; the browser is Alpine's
packaged `chromium` driven over raw CDP (`Page.navigate`, `Page.reload`,
`Runtime.evaluate`) via `py3-websocket-client` (apk). Guide and login are
pure shell (`curl` + `jq` + busybox httpd serving static files refreshed on a
loop). `capture.py` stays Python deliberately: it correlates CDP responses
with async events on a long-lived websocket and supervises the state machine
- in shell that would be the least debuggable code at the most fragile spot.
Do not rewrite it in shell without a concrete reason.

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
tg-<slug>  ── Chromium/Xvfb on Telegram Web ── ffmpeg ──RTMP──┐
tg-<slug>  ── Chromium/Xvfb on Telegram Web ── ffmpeg ──RTMP──┤
                                                             ▼
                                                         MediaMTX
                                                             │
                              ┌──────────────────────────────┴───────────┐
                              │ HLS                          MPEG-TS segments
                              ▼                                          │
                          guide (M3U + XMLTV)                            ▼
                              │                              harvest ──► /library
                              ▼
                     Plex / Jellyfin Live TV
```

Components:

| Name | Count | Base image | Role |
|---|---|---|---|
| `mediamtx` | 1 | `bluenviron/mediamtx` | RTMP in, HLS out, segment recording |
| `tgstream-capture` | N | Playwright python | one channel: detect, join, encode, harvest |
| `tgstream-guide` | 1 | `python:3.12-slim` | merge per-channel `/status` into one M3U + XMLTV |

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

### 3.1 Capture via browser, not MTProto — DECIDED

The mature Telegram call libraries (pytgcalls and relatives) are built for
pushing media *into* group calls, not pulling a broadcast out. The extraction
implementations that exist handle audio and pad the video track with a black
loop. Browser capture is the only route that reliably gets video.

Second reason, specific to this use case: private channels with the
`noforwards` content-protection flag block every API-based download path.
Browser capture is immune.

### 3.2 Live *detection* — OPEN, decide during Phase 1

Two options, and this is the one genuinely unresolved question.

**(a) DOM detection.** Chromium stays resident on the channel and watches for
the live-stream topbar. No API credentials, no session file, one dependency
chain. Cost: ~400MB RSS per idle channel, and the topbar selectors carry both
detection *and* joining — a Telegram Web rename makes the channel go silently
quiet rather than failing loudly.

**(b) MTProto detection (Telethon).** Poll `channels.getFullChannel` and read
`full_chat.call`, accelerated by raw `UpdateGroupCall` events. Idle channels
run no browser at all (~30MB), and detection is robust against UI churn.

The blocker for (b) under container-per-channel: **N containers cannot share
one Telethon session file.** Copies of the same auth key used concurrently earn
`AUTH_KEY_DUPLICATED` and a revoked session. Authorizing each container
separately means N logins and N sessions on the account to answer one boolean.

Resolution: **build Phase 1 with (a)**, but put detection behind a narrow
interface — `is_live(slug) -> (bool, title)` — so Phase 2 can swap it without
touching capture logic. See §4.

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

Known risk, test on first tune: Plex's HDHomeRun client classically expects
raw MPEG-TS from lineup URLs; ours serve MediaMTX HLS. If Plex refuses,
fallback is a copy-remux CGI in the guide (ffmpeg -c copy -f mpegts).

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
| `TG_SLUG` | *required* | `[a-z0-9-]`; MediaMTX path is `tg-<slug>` |
| `TG_PEER` | *required* | channel id, `@username`, or `t.me/+invite` URL |
| `TG_NAME` | slug | display name in the guide |
| `TG_CHANNEL_NUMBER` | `1` | guide ordering |
| `CAPTURE_WIDTH` / `_HEIGHT` / `_FPS` | `1920` / `1080` / `30` | capture geometry |
| `CAPTURE_BITRATE` | `4500k` | video bitrate |
| `CAPTURE_ENCODER` | `cpu` | `cpu` (libx264), `vaapi` (x86 GPU), `v4l2m2m` (Raspberry Pi 4) |
| `VAAPI_DEVICE` | `/dev/dri/renderD128` | render node for `vaapi`; Intel drivers (iHD + i965) are x86-only and installed conditionally on amd64 — arm64 gets Mesa only, so `vaapi` is amd64-only |
| `RECORD` | `true` | write finished streams to `/library` |
| `POLL_INTERVAL` | `5` | seconds between liveness checks |
| `END_GRACE` | `45` | seconds to ride out a flapping stream before ending |
| `JOIN_TIMEOUT` | `45` | seconds to wait for a playing `<video>` |
| `RTMP_URL` | `rtmp://mediamtx:1935` | |
| `PUBLIC_HOST` | — | must resolve from Plex, Jellyfin **and** clients |
| `HLS_PORT` / `HTTP_PORT` | `8888` / `8409` | |
| `DEBUG_VNC` | `false` | expose the browser on 5900 for debugging |

`TG_SLUG` becomes the MediaMTX path *and* the library folder name. Changing it
after recordings exist orphans them.

### 5.2 HTTP endpoints

Capture container, on `HTTP_PORT`:

- `GET /playlist.m3u` — one `#EXTINF` entry for this channel
- `GET /epg.xml` — XMLTV for this channel
- `GET /status` — JSON: `slug`, `name`, `path`, `channel_number`, `state`,
  `title`, `since`, `since_ts`, `url`, `record`, `last_error`
- `GET /login` — QR login page (auto-refreshing QR, 2FA password form)
- `GET /qr.png` — the current mirrored login QR (404 when authorized)
- `POST /login/password` — 2FA cloud password; typed into the page via CDP

`state` is one of `starting | needs-login | idle | joining | live |
join-failed`. In `needs-login` the container also prints the QR to the log
(via vendored jsQR + `qrencode -t UTF8`) on every ~30s token rotation, and
the healthcheck reports unhealthy after 10 minutes of waiting.

Guide container serves `/status`, `/playlist.m3u`, `/epg.xml`, merged.
`UPSTREAMS` is a comma-separated list of capture base URLs.

### 5.3 Volumes

| Path | Mode | Scope |
|---|---|---|
| `/state` | rw | **per channel** — browser profile (Telegram session), slate |
| `/segments` | rw | shared with MediaMTX |
| `/library` | rw | shared; only mounted if `RECORD=true` |

There is no shared `/session` volume: each capture container owns its
Telegram session (integrated QR login, one scan per channel), which avoids
the `AUTH_KEY_DUPLICATED` risk of cloning one auth key into several
concurrently-connected browsers. This supersedes §3.8.

`shm_size: 1gb` is required — Chromium crashes tabs on the default 64MB.

### 5.4 Recording output

```
/tank/media/telegram/<TG_NAME>/<TG_NAME> - YYYY-MM-DD - <Stream Title>.mp4
```

Added to Plex and Jellyfin as a **TV Shows** library, date-based episode
ordering in Plex. Sanitize the stream title for filesystem-hostile characters
and collide-suffix with ` (2)`.

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

- `Dockerfile` — s6-builder → rootfs-builder → `alpine:3.22` main stage with
  pinned apk packages: chromium, ffmpeg, Xvfb, PulseAudio, x11vnc, jq,
  python3, py3-websocket-client, busybox-extras (httpd).
- `root/usr/local/bin/` — `entrypoint` (mode → s6 user bundle),
  `backend-functions`, `make-slate`, `tgstream-healthcheck`.
- `root/etc/s6-overlay/s6-rc.d/` — `init-tgstream` (validate env, clone
  profile, slate), `svc-xvfb`, `svc-pulseaudio` (null sink `tgcap`),
  `svc-x11vnc` (DEBUG_VNC only), `svc-capture`.
- `root/opt/tgstream/capture.py` — detection interface, CDP browser client,
  join, ffmpeg supervisor, harvest, stdlib HTTP endpoints.
- `guide/` — the separate guide image: own `Dockerfile` and `root/` with
  `svc-guide` (shell refresh loop writing /run/guide) and `svc-guide-httpd`
  (busybox httpd), static s6 user bundle, no modes.
- `config/mediamtx.yml`, `docker-compose.yml` (`x-capture` anchor, `login`
  under a compose profile), `.env.example`, `README.md`.

Host paths are **not hardcoded**: `.env` defines `APPDATA_DIR` (session,
per-channel state, segments) and `LIBRARY_DIR` (finished recordings);
`/tank/appdata/tgstream` and `/tank/media/telegram` are only the example
defaults. `TZ` must match between mediamtx and capture containers because
harvest parses local-time segment filenames.

---

## 9. Verifying without a live stream

The hard part to test is the one thing that only happens when a stream starts.

- **Slate path**: bring up one container with no live stream; confirm the
  MediaMTX path publishes and `curl http://<host>:8888/tg-<slug>/index.m3u8`
  returns a playlist.
- **Detection**: `DEBUG_VNC=true`, attach a VNC client, and start a group call
  in a throwaway channel you own. This is also how you refresh the selector
  list when it breaks.
- **Harvest**: independent of Telegram. Drop hand-made TS segments into
  `/segments/tg-<slug>/` with correctly formatted timestamp filenames and call
  the harvest function with a window that overlaps them.
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
