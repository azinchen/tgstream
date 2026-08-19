# tgstream

Watch Telegram channels for live streams (group calls), restream them to Plex
and Jellyfin as live TV, and save each stream to a file for offline viewing.

Works with private channels, including ones with content protection
(`noforwards`): capture is a real Chromium on Telegram Web, screen-grabbed by
ffmpeg — not an API download.

## How it works

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

- **One capture container per channel** (`tg-<slug>`). It idles on the channel
  in a headless Chromium; when a live stream starts it joins, blows the video
  up to full screen, and ffmpeg captures the display and audio into RTMP.
  While idle it publishes a slate loop so the channel never 404s in Plex.
- **MediaMTX** turns RTMP into HLS for the tuners and continuously records
  10-minute MPEG-TS segments (24h retention).
- **guide** merges every capture container's channel into one `playlist.m3u`
  and `epg.xml` — the single tuner URL Plex and Jellyfin want.
- When a stream ends, the segments covering the live window are concatenated
  (`-c copy`, no re-encode) into `/library` as a normal video file.

Two Alpine-based images with pinned package versions, s6-overlay supervised:

- **`ghcr.io/azinchen/tgstream`** (~930MB) — the capture container, with
  integrated QR login (no VNC needed). The browser is Alpine's packaged
  Chromium driven over the DevTools protocol — no Playwright, no pip.
- **`ghcr.io/azinchen/tgstream-guide`** (~20MB) — the guide aggregator: plain
  shell (`curl` + `jq` + busybox httpd), no browser, no Python. Built from
  [`guide/`](guide/).

Released versions are also published to Docker Hub as `azinchen/tgstream`
and `azinchen/tgstream-guide`; both registries carry `linux/amd64` and
`linux/arm64`.

## Setup

### 1. Configure

```sh
cp .env.example .env
```

Edit `.env`: set `PUBLIC_HOST` (an address that resolves from Plex, Jellyfin
**and** every playback client), your channel in `TG_PEER`, and the two host
paths. The `/tank/...` defaults are just a convention — any paths work:
`APPDATA_DIR` holds session/state/segments (not media), `LIBRARY_DIR` is
where finished recordings land and what Plex indexes.

### 2. Start

```sh
docker compose up -d
```

### 3. Log in to Telegram (one QR scan per channel, one time)

On first start each capture container has no Telegram session and prints a
QR code to its log:

```sh
docker logs -f tg-main
```

Scan it with the Telegram app (**Settings → Devices → Link Desktop Device**) —
or open `http://<host>:8410/login` in a browser and scan there. The code
rotates every ~30 seconds; a fresh one is printed on each rotation. Accounts
with a cloud password (2FA) get a password prompt on the `/login` page after
scanning.

The session is saved in the channel's `/state` volume and survives restarts,
recreations and image upgrades. Each channel is its own device in Telegram's
device list — revoke it there to force a re-login (the container returns to
QR mode by itself, and its healthcheck goes unhealthy after 10 minutes of
waiting so you notice).

Verify the slate is up before wiring Plex:

```sh
curl http://<PUBLIC_HOST>:8888/tg-main/index.m3u8   # HLS playlist
curl http://<PUBLIC_HOST>:8409/status               # channel states
```

### 4. Wire Plex / Jellyfin

**Plex** has no native M3U tuner support, so the guide emulates an HDHomeRun
network tuner (`/discover.json`, `/lineup.json`):

1. Settings → Live TV & DVR → Set Up Plex Tuner. No tuner is auto-found;
   click **"Don't see your HDHomeRun device? Enter its network address
   manually"** and enter `<PUBLIC_HOST>:8409` — Plex discovers a
   "tgstream" tuner.
2. On the guide step choose **XMLTV** and enter
   `http://<PUBLIC_HOST>:8409/epg.xml`, then confirm the channel mapping.

The guide's `GUIDE_URL` env must be the URL by which *Plex* reaches the
guide container (default `http://tgstream-guide:8409`, right for the bundled
compose); `TUNER_COUNT` (default `2`) caps concurrent Plex streams.

**Jellyfin** ingests M3U directly: Dashboard → Live TV → add an **M3U
Tuner** with `http://<PUBLIC_HOST>:8409/playlist.m3u` and an **XMLTV**
guide with `http://<PUBLIC_HOST>:8409/epg.xml`.

Pair the tuner while only the slate is live — if the slate tunes and plays,
the live stream will.

Recordings: add `LIBRARY_DIR` as a **TV Shows** library (date-based episode
ordering in Plex). Files are named
`<Name>/<Name> - YYYY-MM-DD - <Stream Title>.mp4`.

## Adding a channel

1. Copy the `tg-main` service block in `docker-compose.yml`; change
   `container_name`, `TG_SLUG`, `TG_PEER`, `TG_NAME`, `TG_CHANNEL_NUMBER`,
   the `/state` volume (`${APPDATA_DIR}/state/<slug>`) and the login port
   (`8411:8409` + `PUBLIC_HTTP_PORT=8411`).
2. Append `http://tg-<slug>:8409` to the guide's `UPSTREAMS`.
3. `docker compose up -d`, then scan the new container's QR once
   (`docker logs -f tg-<slug>`).

`TG_SLUG` becomes the MediaMTX path and the library folder name — changing it
later orphans existing recordings.

## Capture container reference

| Variable | Default | Meaning |
|---|---|---|
| `TG_SLUG` | *required* | `[a-z0-9-]`; MediaMTX path is `tg-<slug>` |
| `TG_PEER` | *required* | channel id, `@username`, or `t.me/+invite` URL |
| `TG_NAME` | slug | display name in the guide |
| `TG_CHANNEL_NUMBER` | `1` | guide ordering |
| `CAPTURE_WIDTH`/`_HEIGHT`/`_FPS` | `1920`/`1080`/`30` | capture geometry |
| `CAPTURE_BITRATE` | `4500k` | video bitrate |
| `CAPTURE_ENCODER` | `cpu` | `cpu` (libx264), `vaapi` (x86 GPU), `v4l2m2m` (Pi 4) |
| `VAAPI_DEVICE` | `/dev/dri/renderD128` | render node used when `CAPTURE_ENCODER=vaapi` |
| `RECORD` | `true` | write finished streams to `/library` |
| `POLL_INTERVAL` | `5` | seconds between liveness checks |
| `END_GRACE` | `45` | seconds to ride out a flapping stream |
| `JOIN_TIMEOUT` | `45` | seconds to wait for a playing `<video>` |
| `DEBUG_VNC` | `false` | expose the browser on port 5900 |

HTTP endpoints on `:8409` (capture and guide alike): `/playlist.m3u`,
`/epg.xml`, `/status`.

## Hardware acceleration

Software encoding costs ~1.5–2 CPU cores per 1080p30 stream; a hardware
encoder drops that to ~0.3. Which one applies depends on the platform:

| Platform | `CAPTURE_ENCODER` | Notes |
|---|---|---|
| x86_64, Intel iGPU | `vaapi` | iHD driver (Gen8+) and i965 (older) included |
| x86_64, AMD integrated APU or discrete Radeon (GCN, ~2012+) | `vaapi` | Mesa `radeonsi` included; VCE/VCN encode block required |
| Older AMD/ATI (pre-GCN) | `cpu` | those cards decode only — no encode hardware |
| NVIDIA | `cpu` | NVENC needs glibc userspace + an nvenc-enabled ffmpeg; impossible on Alpine/musl. `nouveau` has no encode either |
| Raspberry Pi 4 (arm64) | `v4l2m2m` | Pi's own H.264 encoder; ~1080p30 max |
| Raspberry Pi 5, other arm64 | `cpu` | Pi 5 has **no** H.264 hardware encoder |
| anything else / unsure | `cpu` | always works |

### VAAPI (x86_64: Intel / AMD)

The amd64 image ships VAAPI drivers for Intel iGPUs (`intel-media-driver`
for Broadwell/Gen8 and newer, `libva-intel-driver` for older generations)
and for AMD both integrated and discrete (Mesa `radeonsi`, plus `r600` for
decode-only legacy cards). The Intel packages are x86-only, so the arm64
image contains Mesa only — `vaapi` is not a valid choice on arm64.
Whether a GPU can actually *encode* is what the `vainfo` check below
verifies — a driver loading is not the same as an encoder existing.

**1. Check the host has a usable render node:**

```sh
ls /dev/dri
```

You need a `renderD128` (or similar `renderD*`) entry — a bare `card0` with
no render node means no usable GPU encoder on this host, keep `cpu`.

**2. Enable it on the capture container** in `docker-compose.yml`
(the commented lines already in the `tg-main` block):

```yaml
    environment:
      - CAPTURE_ENCODER=vaapi
      # - VAAPI_DEVICE=/dev/dri/renderD129   # only if not renderD128
    devices:
      - /dev/dri:/dev/dri
```

**3. Verify** after `docker compose up -d`, from inside the container:

```sh
docker exec tg-main vainfo                 # driver loads, lists H264 encode profiles
docker logs tg-main | grep h264_vaapi      # encoder in use once a stream is live
```

`vainfo` must list an `H264` entrypoint of type `VAEntrypointEncSlice` —
decode-only GPUs (or very old ones) can't encode; keep `cpu` there.

Troubleshooting:

- **`vainfo` picks the wrong driver** (multi-GPU or unusual hardware): force
  it with `LIBVA_DRIVER_NAME=iHD` (modern Intel), `i965` (old Intel) or
  `radeonsi` (AMD) in the container's environment.
- **ffmpeg dies instantly in vaapi mode**: the container falls back to
  restarting it in the same mode, so a broken VAAPI setup loops — check
  `docker logs` for the ffmpeg error and switch back to `cpu` while
  investigating.
- The slate loop is unaffected either way — it is pre-encoded and
  stream-copied, so the encoder choice only matters while a stream is live.

### V4L2 M2M (Raspberry Pi 4)

The Pi 4's H.264 encoder is exposed via V4L2, not VAAPI:

```yaml
    environment:
      - CAPTURE_ENCODER=v4l2m2m
    devices:
      - /dev/video11:/dev/video11   # bcm2835-codec encoder node
```

Caveats: the Pi 4 encoder tops out around 1080p30 — set
`CAPTURE_WIDTH=1280`/`CAPTURE_HEIGHT=720` if it can't keep up — and the
`vc4`/`v4l2` kernel modules must be enabled (default on Raspberry Pi OS;
check with `ls /dev/video11`). The Pi 5 dropped the H.264 encode block
entirely — use `cpu` there with 720p capture.

## Performance notes

- ~1.5–2 CPU cores per 1080p30 stream with libx264 `veryfast`; VAAPI drops it
  to ~0.3 (see above). Capturing at 720p (`CAPTURE_WIDTH=1280`,
  `CAPTURE_HEIGHT=720`) roughly halves CPU and no phone client will notice.
- ~400MB RAM per idle channel (resident Chromium), ~700MB while capturing.
- End-to-end latency 8–15s.

## Troubleshooting

- **Channel shows `idle` although a stream is live**: Telegram Web renamed the
  live bar. Set `DEBUG_VNC=true` on the container, attach VNC, and refresh the
  selector/marker list at the top of `root/opt/tgstream/capture.py` (the
  clearly marked fragile block).
- **Tuner errors in Plex**: check `curl http://<PUBLIC_HOST>:8888/tg-<slug>/index.m3u8`
  from the Plex host — `PUBLIC_HOST` must resolve there.
- **Recording missing segments**: `TZ` must be identical in `.env` for all
  services; segment selection parses local-time filenames.
- **Chromium tab crashes**: keep `shm_size: 1gb`.
