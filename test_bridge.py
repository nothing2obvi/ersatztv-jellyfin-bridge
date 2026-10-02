import json
import unittest
from unittest.mock import patch, Mock
from bridge import Bridge, playlist, rates

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

    def test_missing_packets_fail_instead_of_inventing_bitrate(self):
        with self.assertRaises(ValueError):
            rates({'streams': [{'index': 0, 'codec_type': 'video'}]})

if __name__ == '__main__':
    unittest.main()
