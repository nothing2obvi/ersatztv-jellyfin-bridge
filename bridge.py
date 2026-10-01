"""On-demand IPTV proxy: stream-copy into NUT with measured BPS tags."""
import hashlib
import json
import logging
import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, urlopen

import yaml

LOG = logging.getLogger('bridge')


def playlist(text, base):
    entries, pending = {}, []
    header = '#EXTM3U'
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith('#EXTM3U'):
            header = line
        elif line.startswith('#'):
            pending.append(line)
        else:
            url = urljoin(base, line)
            if urlsplit(url).scheme not in ('http', 'https'):
                raise ValueError('Only HTTP(S) stream URLs are supported')
            key = hashlib.sha256(url.encode()).hexdigest()[:24]
            entries[key] = (pending, url)
            pending = []
    return header, entries


def rates(data):
    """Use reported per-track rates; otherwise measure payload over media timestamps."""
    result = {}
    for stream in data.get('streams', []):
        index = stream['index']
        if stream.get('codec_type') not in ('video', 'audio'):
            continue
        reported = stream.get('bit_rate') or stream.get('tags', {}).get('BPS')
        if reported and str(reported).isdigit() and int(reported) > 0:
            result[index] = int(reported)
            continue
        packets = [p for p in data.get('packets', [])
                   if p['stream_index'] == index and ('dts_time' in p or 'pts_time' in p)]
        timestamps = [float(p.get('dts_time', p.get('pts_time'))) for p in packets]
        if len(timestamps) < 2:
            raise ValueError('Not enough packets to measure bitrate')
        duration = max(timestamps) - min(timestamps)
        if duration < 1:
            raise ValueError('Sample duration too short')
        result[index] = round(sum(int(p['size']) for p in packets) * 8 / duration)
    if not result:
        raise ValueError('No audio/video tracks found')
    return result


class Bridge:
    def __init__(self, config):
        self.config = config
        self.entries = {}
        self.current_entries = {}
        self.header = '#EXTM3U'
        self.refreshed = 0
        self.cache = {}
        self.lock = threading.Lock()
        self.probe_slots = threading.BoundedSemaphore(config.get('max_concurrent_probes', 2))

    def refresh(self):
        with self.lock:
            if time.monotonic() - self.refreshed < self.config.get('playlist_refresh_seconds', 60):
                return
            with urlopen(Request(self.config['m3u_url'], headers={'User-Agent': 'ersatztv-jellyfin-bridge'}), timeout=15) as response:
                text = response.read(10 * 1024 * 1024 + 1)
                if len(text) > 10 * 1024 * 1024:
                    raise ValueError('Playlist exceeds 10 MB')
                header, entries = playlist(text.decode('utf-8-sig'), response.url)
            self.header = header
            self.entries.update(entries)  # retain URLs for clients using an older playlist
            self.current_entries = entries
            self.refreshed = time.monotonic()
            return entries

    def probe(self, key, url):
        with self.probe_slots:
            cached = self.cache.get(key)
            if cached and time.monotonic() - cached[0] < self.config.get('bitrate_cache_seconds', 300):
                return cached[1]
            args = ['ffprobe', '-v', 'quiet', '-rw_timeout', '15000000',
                    '-analyzeduration', '3000000', '-probesize', '5000000',
                    '-show_streams', '-of', 'json', url]
            data = json.loads(subprocess.run(args, capture_output=True, check=True, timeout=25).stdout)
            tracks = [s for s in data.get('streams', []) if s.get('codec_type') in ('audio', 'video')]
            if any(not s.get('bit_rate') and not s.get('tags', {}).get('BPS') for s in tracks):
                seconds = self.config.get('sample_seconds', 12)
                args[-1:-1] = ['-read_intervals', f'%+{seconds}', '-show_packets',
                              '-show_entries', 'packet=stream_index,dts_time,pts_time,size:stream']
                data = json.loads(subprocess.run(args, capture_output=True, check=True, timeout=seconds + 30).stdout)
            measured = rates(data)
            ordered = [s for kind in ('video', 'audio') for s in data['streams'] if s.get('codec_type') == kind]
            result = (measured, ordered)
            self.cache[key] = (time.monotonic(), result)
            LOG.info('Channel %s: track bitrates %s', key, measured)
            return result


def handler(bridge):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.0'

        def send_text(self, status, text, content_type='text/plain'):
            payload = text.encode()
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == '/health':
                self.send_text(200, 'ok\n')
                return
            if path == '/playlist.m3u':
                try:
                    fresh = bridge.refresh()
                    entries = fresh if fresh is not None else bridge.current_entries
                    base = bridge.config['public_url'].rstrip('/')
                    lines = [bridge.header]
                    for key, (metadata, _) in entries.items():
                        # Upstream per-channel HTTP options would point at the wrong endpoint.
                        lines.extend(line for line in metadata if not line.startswith(('#EXTVLCOPT:', '#KODIPROP:')))
                        lines.append(f'{base}/stream/{key}.nut')
                    self.send_text(200, '\n'.join(lines) + '\n', 'audio/x-mpegurl')
                except Exception:
                    LOG.exception('Playlist refresh failed')
                    self.send_text(502, 'Unable to load upstream playlist\n')
                return
            key = path.removeprefix('/stream/').removesuffix('.nut')
            if not path.startswith('/stream/') or key not in bridge.entries:
                self.send_text(404, 'Unknown channel; load /playlist.m3u first\n')
                return
            url = bridge.entries[key][1]
            process = None
            try:
                measured, ordered = bridge.probe(key, url)
                args = ['ffmpeg', '-nostdin', '-v', 'error', '-rw_timeout', '15000000',
                        '-i', url, '-map', '0:v?', '-map', '0:a?', '-c', 'copy', '-map_metadata', '0']
                # Output stream order is all video tracks followed by all audio tracks.
                # Match that order to ffprobe's source stream indexes.
                for out_index, stream in enumerate(ordered):
                    args += [f'-metadata:s:{out_index}', f"BPS={measured[stream['index']]}"]
                args += ['-f', 'nut', 'pipe:1']
                process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=None)
                first = process.stdout.read(4096)
                if not first or process.poll() not in (None, 0):
                    process.wait(timeout=5)
                    raise RuntimeError('Remuxer produced no output')
            except Exception:
                if process is not None:
                    process.kill()
                    process.wait()
                    process.stdout.close()
                LOG.exception('Unable to start channel %s', key)
                self.send_text(502, 'Unable to probe or remux channel\n')
                return
            try:
                self.send_response(200)
                self.send_header('Content-Type', 'video/x-nut')
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                self.wfile.write(first)
                while chunk := process.stdout.read(65536):
                    self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                process.stdout.close()
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()

    return Handler


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    with open(os.environ.get('CONFIG_PATH', '/app/config.yml')) as source:
        config = yaml.safe_load(source)
    for field in ('m3u_url', 'public_url'):
        if urlsplit(config[field]).scheme not in ('http', 'https'):
            raise ValueError(f'{field} must be an HTTP(S) URL')
    server = ThreadingHTTPServer(('0.0.0.0', int(config.get('port', 8121))), handler(Bridge(config)))
    server.daemon_threads = True
    server.serve_forever()
