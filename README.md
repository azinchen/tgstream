# tgstream

Watch Telegram channels for live streams, restream them to Plex and Jellyfin
as live TV, and save each stream to a file for offline viewing.

Capture is **direct over MTProto** — no browser. Telegram delivers RTMP
broadcasts as ~1-second MPEG-4 chunks; tgstream joins the call, pulls the
chunks, and remuxes them (`-c copy`, no re-encode) into a live HLS + MPEG-TS
stream. Full source quality and framerate, true live edge, ~0.1 CPU per
channel, and it works with private channels including content-protected
(`noforwards`) ones.

## How it works

```
tg-<slug>  ── MTProto: join call, pull 1s chunks ── ffmpeg -c copy ──┐
tg-<slug>  ── MTProto: join call, pull 1s chunks ── ffmpeg -c copy ──┤
                                                                     │ HLS + MPEG-TS
                              ┌──────────────────────────────────────┤
                              │                                      ▼
                          guide (M3U + XMLTV + HDHomeRun)       recordings ──► /library
                              │
                              ▼
                     Plex / Jellyfin Live TV
```

- **One capture container per channel** (`tg-<slug>`). It watches the channel
  over MTProto; when a live stream starts it joins, pulls the chunks, and
  serves a live HLS stream (`/stream.m3u8`) and a continuous MPEG-TS stream
  (`/stream.ts`, for Plex). While idle it serves a slate loop so the channel
  never 404s. When a stream ends, the captured segments are concatenated
  (`-c copy`) into `/library` as a normal video file.
- **guide** merges every capture container into one `playlist.m3u`, `epg.xml`,
  and an emulated HDHomeRun tuner — the single URL Plex and Jellyfin want.

Two Alpine images, no browser, no MediaMTX, no GPU:

- **`ghcr.io/azinchen/tgstream`** (~230MB) — the capture container. Python +
  Telethon (MTProto) + ffmpeg (remux/slate). Integrated QR login.
- **`ghcr.io/azinchen/tgstream-guide`** (~20MB) — the guide aggregator: shell
  + `curl`/`jq` + busybox httpd.

## Setup

### 1. Get Telegram API credentials

Create an app at **https://my.telegram.org → API development tools** and note
the **api_id** and **api_hash**. One pair works for all channels.

### 2. Configure

```sh
cp .env.example .env
```

Edit `.env`: set `API_ID`/`API_HASH`, `PUBLIC_HOST` (an address that resolves
from Plex, Jellyfin **and** every playback client), your channel in `TG_PEER`,
and the two host paths.

### 3. Start and log in (one QR scan per channel, ever)

```sh
docker compose up -d
docker logs -f tg-main
```

On first start the container prints a QR to its log. Scan it with the Telegram
app (**Settings → Devices → Link Desktop Device**) — or open
`http://<host>:8410/login` in a browser and scan there. The MTProto session is
saved in the channel's `/state` volume and survives restarts and upgrades.
Accounts with a cloud password (2FA) get a password prompt on the `/login`
page. The scanning account must be a **member** of the channel.

Verify the slate is up before wiring Plex:

```sh
curl http://<PUBLIC_HOST>:8410/status               # channel state
curl -s http://<PUBLIC_HOST>:8410/stream.m3u8       # HLS playlist
```

### 4. Wire Plex / Jellyfin

**Plex** has no native M3U tuner support, so the guide emulates an HDHomeRun
network tuner:

1. Settings → Live TV & DVR → Set Up Plex Tuner → **"Don't see your
   HDHomeRun device? Enter its network address manually"** → `<PUBLIC_HOST>:8409`.
2. On the guide step choose **XMLTV** →
   `http://<PUBLIC_HOST>:8409/epg.xml` (look for the small *"Have an XMLTV
   guide on your server?"* link, not the predefined lineups), then confirm.

**Jellyfin** ingests M3U directly: Dashboard → Live TV → add an **M3U Tuner**
`http://<PUBLIC_HOST>:8409/playlist.m3u` and an **XMLTV** guide
`http://<PUBLIC_HOST>:8409/epg.xml`.

Pair the tuner while only the slate is live — if the slate tunes and plays,
the live stream will.

Recordings: add `LIBRARY_DIR` as a **TV Shows** library (date-based episode
ordering in Plex). Files are named
`<Name> - YYYY-MM-DD - <Stream Title>.mp4` directly in `LIBRARY_DIR`.

### Watch directly (VLC, etc.)

- Playlist: `http://<PUBLIC_HOST>:8409/playlist.m3u`
- Per channel HLS: `http://<PUBLIC_HOST>:8410/stream.m3u8`
- Per channel MPEG-TS: `http://<PUBLIC_HOST>:8410/stream.ts`

## Adding a channel

1. Copy the `tg-main` service block in `docker-compose.yml`; change
   `container_name`, `TG_SLUG`, `TG_PEER`, `TG_NAME`, `TG_CHANNEL_NUMBER`,
   the `/state` volume (`${APPDATA_DIR}/state/<slug>`) and the published
   port (`8411:8409` + `PUBLIC_HTTP_PORT=8411`).
2. Append `http://tg-<slug>:8409` to the guide's `UPSTREAMS`.
3. `docker compose up -d`, then scan the new container's QR once
   (`docker logs -f tg-<slug>`).

`TG_SLUG` is the stream path and session key — changing it later starts a
fresh channel (new QR login).

## Capture container reference

| Variable | Default | Meaning |
|---|---|---|
| `TG_SLUG` | *required* | `[a-z0-9-]`; stream path is `tg-<slug>` |
| `TG_PEER` | *required* | channel id, `@username`, or `t.me/+invite` URL |
| `API_ID` / `API_HASH` | *required* | from my.telegram.org |
| `TG_NAME` | slug | display name in the guide |
| `TG_CHANNEL_NUMBER` | `1` | guide ordering |
| `PUBLIC_HOST` | `localhost` | resolves from Plex, Jellyfin and clients |
| `PUBLIC_HTTP_PORT` | `HTTP_PORT` | host-published port (for the `/login` URL) |
| `RECORD` | `true` | write finished streams to `/library` |
| `VIDEO_QUALITY` | `2` | Telegram stream quality tier |
| `POLL_INTERVAL` | `5` | seconds between liveness checks |
| `BUFFER_MS` | `2000` | how far behind live to start (latency vs stability) |

HTTP endpoints on the published port: `/stream.m3u8` (HLS), `/stream.ts`
(MPEG-TS), `/s<N>.ts` (segments), `/status`, `/playlist.m3u`, `/epg.xml`,
`/login`, `/qr.png`.

## Volumes

| Path | Mode | Scope |
|---|---|---|
| `/state` | rw | **per channel** — MTProto session, slate |
| `/library` | rw | shared; finished recordings |

Live segments live in `/run/tgstream` (tmpfs), rotated automatically — no
shared segment volume needed.

## Performance

- ~0.1 CPU core per channel (remux is `-c copy`, no decode/encode).
- ~60–120MB RAM per channel.
- End-to-end latency ~4–8s.
- MTProto crypto uses pure-Python AES (`pyaes`); at stream bitrates this is a
  few % of one core.

## Troubleshooting

- **Channel shows `idle` while a stream is live**: confirm the account is a
  member of the channel and the stream is an actual live broadcast (RTMP), not
  a one-off video message.
- **Plex "Could not tune"**: the tuner reads `/stream.ts` (MPEG-TS); check
  `curl http://<PUBLIC_HOST>:8410/stream.ts` from the Plex host resolves.
- **Recording missing**: check `docker logs` for `harvest` errors; `/library`
  must be writable.
