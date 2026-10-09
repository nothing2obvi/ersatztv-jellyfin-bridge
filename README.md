# ersatztv-jellyfin-bridge

**Disclaimer**: This project was vibe-coded with Codex. However, I use it all the time.

## What This Does and Why

Jellyfin mistakes ErsatzTV streams for having a 20 Mbps bitrate even when the
actual bitrate is lower, including when the streams pass through Dispatcharr.
Until [Jellyfin issue #16230](https://github.com/jellyfin/jellyfin/issues/16230)
is fixed, this is a band-aid solution.

The bridge reads an M3U playlist, measures missing stream bitrates, and proxies
the streams with bitrate metadata Jellyfin can read.

The bridge **remuxes, without transcoding**: FFmpeg copies the original encoded
video and audio into a NUT container and adds bitrate metadata. It does not
re-encode the audio or video, so there is no quality loss from the bridge.
Jellyfin may remux the output again for clients, or transcode if client codec
support or playback settings require it.

In my experience, Jellyfin uses Direct Play with this bridge as long as the streams stay below the bitrate limits you set in Jellyfin.

## Important Notes

- Temporary upstream failures during probing or playback startup are retried
  twice, with one- and two-second delays. This may extend startup time when the
  upstream is unavailable; active playback is not restarted.

- Bitrates are sampled averages and cached for five minutes by default. First
  playback can take longer while the bridge measures the stream.
- Set `bitrate_cache_seconds: 0` to keep each channel's first successful bitrate
  until the bridge restarts. **Use with caution:** bitrate can change between
  programs, so the cached value may become inaccurate and cause incorrect
  playback decisions near your Jellyfin bitrate limits. The cache is in memory
  and is cleared on restart.
- Output uses NUT. Allow remuxing in Jellyfin for clients that need it.
- Only HTTP(S) inputs are supported. Subtitles (unless burned-in), data tracks, and per-channel
  HTTP header options are not forwarded.
- Run on a trusted LAN; the proxy has no authentication.
- Keep your real URLs in `config.yml`, which is excluded from Git and the image.

## How to Use

Copy `config.example.yml` to `config.yml`. Set `m3u_url` to your upstream playlist
and `public_url` to the bridge address reachable from Jellyfin, using port 8121.

Create `docker-compose.yml`:

```yaml
services:
  ersatztv-jellyfin-bridge:
    image: ghcr.io/nothing2obvi/ersatztv-jellyfin-bridge:latest
    ports:
      - "8121:8121"
    volumes:
      - ./config.yml:/app/config.yml:ro
    restart: unless-stopped
    init: true
```

Run `docker compose up -d`, then add
`http://YOUR-BRIDGE-HOST:8121/playlist.m3u` as an M3U tuner in Jellyfin.
Restart the container after changing configuration.
