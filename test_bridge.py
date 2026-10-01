import unittest
from bridge import playlist, rates

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

    def test_missing_packets_fail_instead_of_inventing_bitrate(self):
        with self.assertRaises(ValueError):
            rates({'streams': [{'index': 0, 'codec_type': 'video'}]})

if __name__ == '__main__':
    unittest.main()
