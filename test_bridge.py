import json
import subprocess
import unittest
from unittest.mock import patch, Mock
from bridge import Bridge, handler, playlist, rates, probe_json, start_remux

class BridgeTests(unittest.TestCase):
    def test_playlist_preserves_channel_metadata_and_resolves_relative_urls(self):
        header, entries = playlist('#EXTM3U x-tvg-url="http://guide/epg.xml"\n#EXTINF:-1 tvg-id="a",Channel A\n/stream/a\n', 'http://upstream/list')
        self.assertIn('x-tvg-url', header)
        metadata, url = next(iter(entries.values()))
        self.assertEqual(url, 'http://upstream/stream/a')
        self.assertEqual(metadata, ['#EXTINF:-1 tvg-id="a",Channel A'])

    def test_rates_uses_media_time_instead_of_download_time(self):
        data = {'streams': [{'index': 0, 'codec_type': 'video'}, {'index': 1, 'codec_type': 'audio', 'bit_rate': '192000'}], 'packets': [
            {'stream_index': 0, 'dts_time': '10', 'size': '500000'},
            {'stream_index': 0, 'dts_time': '12', 'size': '500000'}]}
        self.assertEqual(rates(data), {0: 4000000, 1: 192000})

    def test_zero_cache_keeps_first_successful_result(self):
        bridge = Bridge({'bitrate_cache_seconds': 0})
        response = Mock(stdout=json.dumps({'streams': [
            {'index': 0, 'codec_type': 'video', 'bit_rate': '7000000'}]}).encode())
        with patch('bridge.subprocess.run', return_value=response) as probe, patch('bridge.time.monotonic', return_value=10):
            first = bridge.probe('channel', 'http://upstream.example/stream')
        with patch('bridge.subprocess.run') as probe, patch('bridge.time.monotonic', return_value=10000000):
            self.assertIs(bridge.probe('channel', 'http://upstream.example/stream'), first)
            probe.assert_not_called()

    def test_positive_cache_expires_and_refreshes(self):
        bridge = Bridge({'bitrate_cache_seconds': 300})
        bridge.cache['channel'] = (10, ({0: 4000000}, []))
        response = Mock(stdout=json.dumps({'streams': [
            {'index': 0, 'codec_type': 'video', 'bit_rate': '7000000'}]}).encode())
        with patch('bridge.subprocess.run', return_value=response) as probe, patch('bridge.time.monotonic', return_value=310):
            result = bridge.probe('channel', 'http://upstream.example/stream')
            self.assertEqual(result[0], {0: 7000000})
            probe.assert_called_once()

    def test_stream_request_reloads_channels_after_restart(self):
        bridge = Bridge({})
        request = object.__new__(handler(bridge))
        request.path = '/stream/channel.nut'
        request.send_text = Mock()
        def reload():
            bridge.entries['channel'] = ([], 'http://upstream.example/stream')
        bridge.refresh = Mock(side_effect=reload)
        bridge.probe = Mock(side_effect=RuntimeError('stop before remux'))
        with patch('bridge.LOG.exception'):
            request.do_GET()
        bridge.refresh.assert_called_once()
        bridge.probe.assert_called_once_with('channel', 'http://upstream.example/stream')
        self.assertEqual(request.send_text.call_args.args[0], 502)

    def test_probe_retries_temporary_error_then_succeeds(self):
        failure = subprocess.CalledProcessError(1, ['ffprobe'], stderr=b'HTTP error 503 Service Unavailable')
        with patch('bridge.subprocess.run', side_effect=[failure, Mock(stdout=b'{"streams": []}')]) as run, patch('bridge.time.sleep') as sleep:
            self.assertEqual(probe_json(['ffprobe'], 25, 'channel'), {'streams': []})
            self.assertEqual(run.call_count, 2)
            sleep.assert_called_once_with(1)

    def test_probe_retries_are_bounded(self):
        failure = subprocess.TimeoutExpired(['ffprobe'], 25)
        with patch('bridge.subprocess.run', side_effect=failure) as run, patch('bridge.time.sleep') as sleep:
            with self.assertRaises(RuntimeError):
                probe_json(['ffprobe'], 25, 'channel')
            self.assertEqual(run.call_count, 3)
            self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 2])

    def test_probe_does_not_retry_missing_channel(self):
        failure = subprocess.CalledProcessError(1, ['ffprobe'], stderr=b'HTTP error 404 Not Found')
        with patch('bridge.subprocess.run', side_effect=failure) as run, patch('bridge.time.sleep') as sleep:
            with self.assertRaises(RuntimeError):
                probe_json(['ffprobe'], 25, 'channel')
            run.assert_called_once()
            sleep.assert_not_called()

    def test_remux_retries_startup_and_closes_failed_process(self):
        failed = Mock()
        failed.stdout.read.return_value = b''
        working = Mock()
        working.stdout.read.return_value = b'NUT stream bytes'
        working.poll.return_value = None
        def launch(*args, **kwargs):
            if launch.first:
                launch.first = False
                kwargs['stderr'].write(b'Server returned 5XX Server Error reply')
                return failed
            return working
        launch.first = True
        with patch('bridge.subprocess.Popen', side_effect=launch) as popen, patch('bridge.time.sleep') as sleep:
            process, first = start_remux(['ffmpeg'], 'channel')
            self.assertIs(process, working)
            self.assertEqual(first, b'NUT stream bytes')
            self.assertEqual(popen.call_count, 2)
            failed.terminate.assert_called_once()
            failed.stdout.close.assert_called_once()
            sleep.assert_called_once_with(1)

    def test_missing_packets_fail_instead_of_inventing_bitrate(self):
        with self.assertRaises(ValueError):
            rates({'streams': [{'index': 0, 'codec_type': 'video'}]})

if __name__ == '__main__':
    unittest.main()
