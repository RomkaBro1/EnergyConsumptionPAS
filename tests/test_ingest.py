import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch
import pandas as pd

from gridpulse.config import load_config
from gridpulse.ingest import Downloader, weather_watermark
from gridpulse.storage import Store


class Response:
    status = 200
    headers = {"ETag": "test-v1"}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self):
        return b'{"value": 42}'


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name))
        self.cfg = load_config()
        self.cfg["http_attempts"] = 2
        self.cfg["energy_request_gap"] = 0
        self.downloader = Downloader(self.store, self.cfg, "test")

    def tearDown(self):
        self.temp.cleanup()

    def test_delayed_solar_publication_uses_its_own_watermark(self):
        observations = pd.DataFrame({"timestamp_utc": ["2026-08-31T23:00Z", "2026-09-29T23:00Z"],
                                     "temperature_c": [15., 10.], "solar_energy_wh_m2": [30., None]})
        self.assertEqual(weather_watermark(observations, "solar_energy_wh_m2", self.cfg), pd.Timestamp("2026-08-24T23:00Z"))
        self.assertEqual(weather_watermark(observations, "temperature_c", self.cfg), pd.Timestamp("2026-09-22T23:00Z"))

    @patch("gridpulse.ingest.time.sleep")
    @patch("gridpulse.ingest.urllib.request.urlopen")
    def test_retry_cache_and_raw_hash(self, urlopen, sleep):
        urlopen.side_effect = [TimeoutError("simulated"), Response()]
        body, sha = self.downloader.get("https://example.test/data", "test", ttl=60)
        self.assertEqual(json.loads(body)["value"], 42)
        self.assertTrue((self.store.root / "bronze/objects" / sha).exists())
        self.assertEqual(urlopen.call_count, 2)
        cached = self.downloader.get("https://example.test/data", "test", ttl=60)
        self.assertEqual(cached, (body, sha))
        self.assertEqual(urlopen.call_count, 2)
        self.assertEqual([r["status"] for r in self.store.query("SELECT status FROM journal")], ["error", "ok", "cache"])

    @patch("gridpulse.ingest.time.sleep")
    @patch("gridpulse.ingest.urllib.request.urlopen")
    def test_429_retry_after_is_honored(self, urlopen, sleep):
        urlopen.side_effect = [urllib.error.HTTPError("https://example.test", 429, "Too many requests", {"Retry-After": "3"}, None), Response()]
        self.downloader.get("https://example.test/data", "test")
        sleep.assert_any_call(3)
        self.assertEqual(urlopen.call_count, 2)

    @patch("gridpulse.ingest.time.sleep")
    @patch("gridpulse.ingest.urllib.request.urlopen")
    def test_expired_cache_is_not_silently_reported_as_fresh(self, urlopen, sleep):
        urlopen.return_value = Response()
        original = self.downloader.get("https://example.test/data", "test")
        urlopen.side_effect = TimeoutError("offline")
        with self.assertRaises(TimeoutError):
            self.downloader.get("https://example.test/data", "test", ttl=0)
        self.assertTrue((self.store.root / "bronze/objects" / original[1]).exists())

    @patch("gridpulse.ingest.time.sleep")
    @patch("gridpulse.ingest.urllib.request.urlopen")
    def test_304_preserves_response(self, urlopen, sleep):
        urlopen.return_value = Response()
        original = self.downloader.get("https://example.test/data", "test")
        urlopen.side_effect = urllib.error.HTTPError("https://example.test", 304, "Not modified", {}, None)
        self.assertEqual(original, self.downloader.get("https://example.test/data", "test"))


if __name__ == "__main__":
    unittest.main()
