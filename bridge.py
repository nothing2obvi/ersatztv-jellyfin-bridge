"""On-demand IPTV proxy: stream-copy into NUT with bitrate metadata."""
import hashlib
import json
import logging
import os
import re
import subprocess
import threading
import tempfile
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, urlopen

import yaml

LOG = logging.getLogger('bridge')


RETRY_DELAYS = (1, 2)
BITRATE_MODES = ('auto', 'fixed', 'fallback')


def ffmpeg_input_timeout_us(config):
    """Validate seconds and convert to FFmpeg's microseconds."""
    value = config.get('ffmpeg_input_timeout_seconds', 30)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not (0 < value <= 3600):
        raise ValueError('ffmpeg_input_timeout_seconds must be a number greater than 0 and at most 3600')
    return str(round(value * 1_000_000))


def ffmpeg_analysis_args(config):
    """Bound input inspection to reduce channel startup latency.

    FFmpeg option units: analyzeduration in microseconds, probesize in bytes.
    """
    seconds = config.get('ffmpeg_analyzeduration_seconds', 1)
    size = config.get('ffmpeg_probesize_bytes', 1_000_000)
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not (0 < seconds <= 120):
        raise ValueError('ffmpeg_analyzeduration_seconds must be greater than 0 and at most 120')
    if isinstance(size, bool) or not isinstance(size, int) or not (32 <= size <= 100_000_000):
        raise ValueError('ffmpeg_probesize_bytes must be an integer from 32 to 100000000')
    return ['-analyzeduration', str(round(seconds * 1_000_000)), '-probesize', str(size)]


def validate_bitrate_config(config):
    mode = config.get('bitrate_mode', 'auto')
    if mode not in BITRATE_MODES:
        raise ValueError(f'bitrate_mode must be one of: {", ".join(BITRATE_MODES)}')
    if mode in ('fixed', 'fallback'):
        for field in ('video_bitrate', 'audio_bitrate'):
            value = config.get(field)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f'{field} must be a positive integer (bits per second) in {mode} mode')
    return mode


def fixed_metadata_args(config):
    """Set a bitrate hint for each mapped video/audio track without probing."""
    return ['-metadata:s:v', f"BPS={config['video_bitrate']}",
            '-metadata:s:a', f"BPS={config['audio_bitrate']}"]


def measured_metadata_args(measured, ordered):
    args = []
    for out_index, stream in enumerate(ordered):
        args += [f'-metadata:s:{out_index}', f"BPS={measured[stream['index']]}"]
    return args


def safe_ffmpeg_diagnostics(detail, args):
    """Return useful FFmpeg messages while suppressing URLs and possible secrets.

    When a line contains a URL or credential-looking field, discard that whole
    line. This deliberately favors protecting IPTV credentials over diagnostics.
    """
    private_args = [arg for arg in args if isinstance(arg, str) and
                    (arg.startswith(('http://', 'https://')) or '://' in arg)]
    sanitized = []
    for line in detail.splitlines()[-30:]:
        if any(value in line for value in private_args) or re.search(
                r'(?i)(?:https?://|authorization|bearer\s|password|passwd|token|api[_-]?key|username|cookie|\buser=|\bpass=)', line):
            sanitized.append('[FFmpeg diagnostic line redacted: URL or possible credential]')
        else:
            sanitized.append(line[:500])
    return '\n'.join(sanitized)[-4000:] or '[FFmpeg produced no diagnostic output]'


def temporary_error(detail):
    detail = detail.lower()
    return bool(re.search(r'http error 5\d\d|server returned 5\d\d|server returned 5xx|http error 429', detail)) or any(
        message in detail for message in ('operation timed out', 'timed out', 'connection timed out', 'connection refused',
                                         'connection reset', 'input/output error', 'i/o error'))


def probe_json(args, timeout, key):
    for attempt in range(len(RETRY_DELAYS) + 1):
        try:
            return json.loads(subprocess.run(args, capture_output=True, check=True, timeout=timeout).stdout)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
            detail = (error.stderr or b'').decode(errors='replace')
            transient = isinstance(error, subprocess.TimeoutExpired) or temporary_error(detail)
            if not transient or attempt == len(RETRY_DELAYS):
                # Avoid including the input URL (which may contain credentials) in the exception.
                LOG.error('Channel %s: probe failed:\n%s', key, safe_ffmpeg_diagnostics(detail, args))
                reason = 'probe timed out' if isinstance(error, subprocess.TimeoutExpired) else 'probe exited with an error'
                raise RuntimeError(reason) from None
            delay = RETRY_DELAYS[attempt]
            LOG.warning('Channel %s: temporary probe failure; retrying in %ss', key, delay)
            time.sleep(delay)


def stop_process(process):
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
    process.stdout.close()


def start_remux(args, key):
    """Retry startup only, before any bytes or HTTP headers reach the client."""
    for attempt in range(len(RETRY_DELAYS) + 1):
        attempt_start = time.monotonic()
        with tempfile.TemporaryFile() as errors:
            process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=errors)
            try:
                first = process.stdout.read1(4096)
                if first and process.poll() in (None, 0):
                    LOG.info('Channel %s: FFmpeg first output after %.3fs (attempt %d)',
                             key, time.monotonic() - attempt_start, attempt + 1)
                    return process, first
            except BaseException:
                stop_process(process)
                raise
            stop_process(process)
            errors.seek(0, os.SEEK_END)
            size = errors.tell()
            errors.seek(max(0, size - 16384))
            detail = errors.read().decode(errors='replace')
        LOG.warning('Channel %s: FFmpeg attempt %d failed after %.3fs',
                    key, attempt + 1, time.monotonic() - attempt_start)
        if not temporary_error(detail) or attempt == len(RETRY_DELAYS):
            LOG.error('Channel %s: FFmpeg startup failed (exit code %s):\n%s',
                      key, process.returncode,
                      safe_ffmpeg_diagnostics(detail, args))
            raise RuntimeError('Remuxer failed to start')
        delay = RETRY_DELAYS[attempt]
        LOG.warning('Channel %s: temporary playback startup failure; retrying in %ss', key, delay)
        time.sleep(delay)


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
        self.bitrate_mode = validate_bitrate_config(config)
        self.ffmpeg_timeout_us = ffmpeg_input_timeout_us(config)
        self.ffmpeg_analysis = ffmpeg_analysis_args(config)
        self.entries = {}
        self.current_entries = {}
        self.header = '#EXTM3U'
        self.refreshed = 0
        self.cache = {}
        self.probe_locks = {}
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
        with self.lock:
            channel_lock = self.probe_locks.setdefault(key, threading.Lock())
        with channel_lock:
            cached = self.cache.get(key)
            cache_seconds = self.config.get('bitrate_cache_seconds', 300)
            if cached and (cache_seconds == 0 or time.monotonic() - cached[0] < cache_seconds):
                return cached[1]
            with self.probe_slots:
                try:
                    args = ['ffprobe', '-v', 'error', '-rw_timeout', '15000000',
                            '-analyzeduration', '3000000', '-probesize', '5000000',
                            '-show_streams', '-of', 'json', url]
                    data = probe_json(args, 25, key)
                    tracks = [s for s in data.get('streams', []) if s.get('codec_type') in ('audio', 'video')]
                    if any(not s.get('bit_rate') and not s.get('tags', {}).get('BPS') for s in tracks):
                        seconds = self.config.get('sample_seconds', 12)
                        args[-1:-1] = ['-read_intervals', f'%+{seconds}', '-show_packets',
                                      '-show_entries', 'packet=stream_index,dts_time,pts_time,size:stream']
                        data = probe_json(args, seconds + 30, key)
                    measured = rates(data)
                    ordered = [s for kind in ('video', 'audio') for s in data['streams'] if s.get('codec_type') == kind]
                    result = (measured, ordered)
                    self.cache[key] = (time.monotonic(), result)
                    LOG.info('Channel %s: track bitrates %s', key, measured)
                    return result
                except (RuntimeError, ValueError) as error:
                    if cached is not None:
                        LOG.warning('Channel %s: probing failed (%s); using expired cached bitrates', key, error)
                        return cached[1]
                    raise


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
            if not path.startswith('/stream/'):
                self.send_text(404, 'Unknown endpoint\n')
                return
            if key not in bridge.entries:
                try:
                    bridge.refresh()
                except Exception:
                    LOG.exception('Unable to reload channels for stream request')
                    self.send_text(502, 'Unable to load upstream playlist\n')
                    return
            if key not in bridge.entries:
                self.send_text(404, 'Unknown channel\n')
                return
            url = bridge.entries[key][1]
            request_start = time.monotonic()
            process = None
            try:
                args = ['ffmpeg', '-nostdin', '-v', 'error', '-rw_timeout', bridge.ffmpeg_timeout_us]
                args += bridge.ffmpeg_analysis
                args += ['-i', url, '-map', '0:v?', '-map', '0:a?', '-c', 'copy', '-map_metadata', '0']
                if bridge.bitrate_mode == 'fixed':
                    metadata = fixed_metadata_args(bridge.config)
                else:
                    try:
                        measured, ordered = bridge.probe(key, url)
                        metadata = measured_metadata_args(measured, ordered)
                    except (RuntimeError, ValueError) as error:
                        if bridge.bitrate_mode != 'fallback':
                            raise
                        LOG.warning('Channel %s: probe failed (%s); using configured bitrates', key, error)
                        metadata = fixed_metadata_args(bridge.config)
                args += metadata + ['-f', 'nut', 'pipe:1']
                process, first = start_remux(args, key)
                LOG.info('Channel %s: request to first NUT bytes %.3fs',
                         key, time.monotonic() - request_start)
            except Exception:
                LOG.warning('Channel %s: startup failed after %.3fs',
                            key, time.monotonic() - request_start)
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
                LOG.info('Channel %s: first NUT bytes delivered after %.3fs',
                         key, time.monotonic() - request_start)
                while chunk := process.stdout.read1(65536):
                    self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                stop_process(process)

    return Handler


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    with open(os.environ.get('CONFIG_PATH', '/app/config.yml')) as source:
        config = yaml.safe_load(source)
    for field in ('m3u_url', 'public_url'):
        if urlsplit(config[field]).scheme not in ('http', 'https'):
            raise ValueError(f'{field} must be an HTTP(S) URL')
    bridge = Bridge(config)
    LOG.info('Bitrate mode: %s', bridge.bitrate_mode)
    LOG.info('FFmpeg input timeout: %s seconds', config.get('ffmpeg_input_timeout_seconds', 30))
    LOG.info('FFmpeg input analysis: %ss, probe size %s bytes',
             config.get('ffmpeg_analyzeduration_seconds', 1), config.get('ffmpeg_probesize_bytes', 1_000_000))
    server = ThreadingHTTPServer(('0.0.0.0', int(config.get('port', 8121))), handler(bridge))
    server.daemon_threads = True
    server.serve_forever()
