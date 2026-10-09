import json
import subprocess
import unittest
from unittest.mock import patch, Mock
from bridge import Bridge, handler, playlist, rates, probe_json, start_remux, fixed_metadata_args, ffmpeg_input_timeout_us, safe_ffmpeg_diagnostics

class BridgeTests(unittest.TestCase):
    def test_default_ffmpeg_timeout_is_thirty_seconds(self):
        self.assertEqual(Bridge({}).ffmpeg_timeout_us, '30000000')

    def test_configurable_ffmpeg_timeout_seconds(self):
        self.assertEqual(Bridge({'ffmpeg_input_timeout_seconds': 45}).ffmpeg_timeout_us, '45000000')
        self.assertEqual(Bridge({'ffmpeg_input_timeout_seconds': 2.5}).ffmpeg_timeout_us, '2500000')

    def test_invalid_ffmpeg_timeout_rejected(self):
        for invalid in (0, -1, True, '30', 3601, float('nan')):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                Bridge({'ffmpeg_input_timeout_seconds': invalid})

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
        failed.stdout.read1.return_value = b''
        working = Mock()
        working.stdout.read1.return_value = b'NUT stream bytes'
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
            working.stdout.read1.assert_called_once_with(4096)
            working.stdout.read.assert_not_called()
            self.assertEqual(popen.call_count, 2)
            failed.terminate.assert_called_once()
            failed.stdout.close.assert_called_once()
            sleep.assert_called_once_with(1)

    def test_cached_channel_does_not_wait_for_probe_slot(self):
        bridge = Bridge({'bitrate_cache_seconds': 0})
        cached = ({0: 7000000}, [])
        bridge.cache['channel'] = (0, cached)
        class NoProbeAllowed:
            def __enter__(self):
                raise AssertionError('cached playback must not acquire a probe slot')
        bridge.probe_slots = NoProbeAllowed()
        self.assertIs(bridge.probe('channel', 'http://upstream.example/stream'), cached)

    def test_diagnostics_redact_private_urls_and_credentials(self):
        url = 'http://upstream.example/secret'
        output = safe_ffmpeg_diagnostics('Error opening ' + url + '\nHTTP error 503\nAuthorization: secret', [url])
        self.assertNotIn(url, output)
        self.assertNotIn('secret', output)
        self.assertIn('HTTP error 503', output)

    def test_missing_packets_fail_instead_of_inventing_bitrate(self):
        with self.assertRaises(ValueError):
            rates({'streams': [{'index': 0, 'codec_type': 'video'}]})

    def test_fixed_mode_skips_probe_and_sets_metadata(self):
        bridge = Bridge({'bitrate_mode': 'fixed', 'video_bitrate': 6000000, 'audio_bitrate': 192000})
        bridge.entries['channel'] = ([], 'http://upstream.example/stream')
        request = object.__new__(handler(bridge))
        request.path = '/stream/channel.nut'
        request.send_response = Mock()
        request.send_header = Mock()
        request.end_headers = Mock()
        request.wfile = Mock()
        process = Mock()
        process.stdout.read1.return_value = b''
        with patch.object(bridge, 'probe', side_effect=AssertionError('must not probe')) as probe, \
                patch('bridge.start_remux', return_value=(process, b'first')) as remux, \
                patch('bridge.stop_process'):
            request.do_GET()
        probe.assert_not_called()
        args = remux.call_args.args[0]
        self.assertIn('-metadata:s:v', args)
        self.assertIn('BPS=6000000', args)
        self.assertIn('-metadata:s:a', args)
        self.assertIn('BPS=192000', args)
        self.assertEqual(request.send_response.call_args.args[0], 200)

    def test_fallback_mode_uses_fixed_metadata_on_probe_failure(self):
        bridge = Bridge({'bitrate_mode': 'fallback', 'video_bitrate': 7000000, 'audio_bitrate': 128000})
        bridge.entries['channel'] = ([], 'http://upstream.example/stream')
        request = object.__new__(handler(bridge))
        request.path = '/stream/channel.nut'
        request.send_response = Mock()
        request.send_header = Mock()
        request.end_headers = Mock()
        request.wfile = Mock()
        process = Mock()
        process.stdout.read1.return_value = b''
        with patch.object(bridge, 'probe', side_effect=RuntimeError('probe exited with an error')), \
                patch('bridge.start_remux', return_value=(process, b'first')) as remux, \
                patch('bridge.stop_process'):
            request.do_GET()
        self.assertIn('BPS=7000000', remux.call_args.args[0])
        self.assertIn('BPS=128000', remux.call_args.args[0])
        self.assertEqual(request.send_response.call_args.args[0], 200)

    def test_probe_failure_uses_stale_cache(self):
        bridge = Bridge({'bitrate_mode': 'auto', 'bitrate_cache_seconds': 1})
        prior = ({0: 4000000}, [{'index': 0, 'codec_type': 'video'}])
        bridge.cache['channel'] = (10, prior)
        with patch('bridge.time.monotonic', return_value=20), \
                patch('bridge.subprocess.run', side_effect=subprocess.CalledProcessError(1, ['ffprobe'])):
            self.assertEqual(bridge.probe('channel', 'http://upstream.example/stream'), prior)

    def test_invalid_mode_and_rates_fail_fast(self):
        for config in ({'bitrate_mode': 'none'}, {'bitrate_mode': 'fixed'},
                       {'bitrate_mode': 'fallback', 'video_bitrate': 1, 'audio_bitrate': 0},
                       {'bitrate_mode': 'fixed', 'video_bitrate': True, 'audio_bitrate': 1}):
            with self.subTest(config=config), self.assertRaises(ValueError):
                Bridge(config)

    def test_legacy_mode_defaults_to_auto(self):
        self.assertEqual(Bridge({}).bitrate_mode, 'auto')

class LatencyConfigTests(unittest.TestCase):
    def test_defaults(self):
        bridge = Bridge({})
        self.assertEqual(bridge.ffmpeg_analysis, ['-analyzeduration', '1000000', '-probesize', '1000000'])

    def test_custom_settings(self):
        bridge = Bridge({'ffmpeg_analyzeduration_seconds': 2.5, 'ffmpeg_probesize_bytes': 3000000})
        self.assertEqual(bridge.ffmpeg_analysis, ['-analyzeduration', '2500000', '-probesize', '3000000'])

    def test_reject_invalid_settings(self):
        for settings in ({'ffmpeg_analyzeduration_seconds': -1}, {'ffmpeg_analyzeduration_seconds': '1'},
                         {'ffmpeg_analyzeduration_seconds': float('nan')}, {'ffmpeg_probesize_bytes': 0},
                         {'ffmpeg_probesize_bytes': '1000000'}, {'ffmpeg_probesize_bytes': True}):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                Bridge(settings)

    def test_timeout_error_retryable(self):
        from bridge import temporary_error
        self.assertTrue(temporary_error('Error opening input: Operation timed out'))


if __name__ == "__main__":
    unittest.main()
