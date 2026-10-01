# ersatztv-jellyfin-bridge

## What this does and why

Jellyfin mistakes ErsatzTV streams for having a 20 Mbps bitrate even when the
actual bitrate is lower, including when the streams pass through Dispatcharr.
Until [Jellyfin issue #16230](https://github.com/jellyfin/jellyfin/issues/16230)
is fixed, this is a band-aid solution.

The bridge reads an M3U playlist, measures missing stream bitrates, and proxies
the streams with bitrate metadata Jellyfin can read.

The bridge **remuxes, without transcoding**: FFmpeg copies the original encoded
video and audio into a NUT container and adds bitrate metadata. It does not
re-encode the audio or video, so there is no quality loss from the bridge.
Jellyfin generally remuxes the output again for clients; it may still transcode
if client codec support or playback settings require it.

## important notes

- Bitrates are sampled averages and cached for five minutes. First playback can
  take longer while the bridge measures the stream.
- Output uses NUT. Jellyfin generally needs to remux it for clients; allow remuxing.
- Only HTTP(S) inputs are supported. Subtitles, data tracks, and per-channel
  HTTP header options are not forwarded.
- Run on a trusted LAN; the proxy has no authentication.
- Keep your real URLs in `config.yml`, which is excluded from Git and the image.

## how to use

Copy `config.example.yml` to `config.yml`. Set `m3u_url` to your upstream playlist
and `public_url` to the bridge address reachable from Jellyfin, using port 8121.

Create `docker-compose.yml`:

```yaml
services:
  ersatztv-jellyfin-bridge:
    image: ghcr.io/nothing2obvi/ersatztv-jellyfin-bridge:v0.1.0
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
