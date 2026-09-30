import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

from gridpulse.calendar import calendar_days
from gridpulse.config import load_config
from gridpulse.data import build_mart
from gridpulse.model import features, metrics, weather_for
from gridpulse.parsers import hourly_load, parse_energy
from gridpulse.pipeline import pipeline_lock
from gridpulse.service import create_app
from gridpulse.storage import Store


class EnergyTests(unittest.TestCase):
    def intervals(self):
        return pd.DataFrame({"timestamp_utc": pd.date_range("2025-01-01", periods=4, freq="15min", tz="UTC"),
                             "load_mw": [100., 100., 100., 100.], "interval_seconds": [900] * 4})

    def test_power_to_hourly_energy(self):
        result = hourly_load(self.intervals())
        self.assertEqual(result.load_energy_mwh.iloc[0], 100)
        self.assertEqual(result.covered_seconds.iloc[0], 3600)

    def test_incomplete_hour_not_published(self):
        frame = self.intervals()
        frame.loc[2, "load_mw"] = np.nan
        self.assertTrue(pd.isna(hourly_load(frame).load_mean_mw.iloc[0]))

    def test_duplicate_and_overlapping_intervals_rejected(self):
        frame = self.intervals()
        with self.assertRaises(ValueError):
            hourly_load(pd.concat([frame, frame.iloc[:1]]))
        frame.loc[0, "interval_seconds"] = 1800
        with self.assertRaises(ValueError):
            hourly_load(frame)

    def test_exact_load_series_and_half_open_bounds(self):
        start = pd.Timestamp("2025-01-01", tz="UTC")
        stamps = pd.date_range(start, periods=5, freq="15min")
        payload = {"unix_seconds": [t.timestamp() for t in stamps], "production_types": [
            {"name": "Residual load", "data": [1] * 5}, {"name": "Load", "data": [100] * 5}]}
        out = parse_energy(json.dumps(payload), "hash", "url", start, start + pd.Timedelta(hours=1))
        self.assertEqual(len(out), 4)
        self.assertEqual(out.load_mw.sum(), 400)
        payload["deprecated"] = True
        with self.assertRaises(ValueError):
            parse_energy(json.dumps(payload), "hash", "url", start, stamps[-1])

    def test_dst_has_23_and_25_unique_hours(self):
        for day, count in [("2025-03-30", 23), ("2025-10-26", 25)]:
            start = pd.Timestamp(day, tz="Europe/Berlin")
            index = pd.date_range(start, start + pd.DateOffset(days=1), inclusive="left", freq="h").tz_convert("UTC")
            self.assertEqual(len(index), count)
            self.assertTrue(index.is_unique)

    def test_calendar_national_and_regional(self):
        cal = calendar_days("2025-01-01", "2025-12-31").set_index("date_local")
        self.assertEqual(cal.loc["2025-10-03", "is_national_holiday"], 1)
        self.assertEqual(cal.loc["2025-10-03", "workday_fraction"], 0)
        self.assertEqual(cal.loc["2025-01-02", "workday_fraction"], 1)
        self.assertLess(cal.loc["2025-01-06", "workday_fraction"], 1)
        self.assertEqual(cal.loc["2025-01-04", "workday_fraction"], 0)


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name))

    def tearDown(self):
        self.temp.cleanup()

    def test_idempotence_and_historization(self):
        f = pd.DataFrame({"id": [1, 2], "value": [10., 20.], "raw_sha256": ["a", "a"]})
        self.assertEqual(self.store.upsert("x", f, ["id"], "r1")["new"], 2)
        self.assertEqual(self.store.upsert("x", f, ["id"], "r2")["unchanged"], 2)
        f.loc[0, "value"] = 11
        self.assertEqual(self.store.upsert("x", f, ["id"], "r3")["changed"], 1)
        old = self.store.query("SELECT * FROM history")
        self.assertEqual(len(old), 1)
        self.assertEqual(json.loads(old[0]["payload"])["value"], 10)
        self.assertEqual(len(self.store.frame("x")), 2)

    def test_invalid_batch_does_not_replace_existing(self):
        self.store.upsert("x", pd.DataFrame({"id": [1], "value": [5]}), ["id"], "r1")
        with self.assertRaises(ValueError):
            self.store.upsert("x", pd.DataFrame({"id": [1, 1], "value": [8, 9]}), ["id"], "r2")
        self.assertEqual(self.store.frame("x").value.iloc[0], 5)

    def test_process_lock_blocks_second_writer(self):
        with pipeline_lock(self.store):
            with self.assertRaises(RuntimeError):
                with pipeline_lock(self.store):
                    pass
        with pipeline_lock(self.store):
            pass

    def test_mart_preserves_gaps_and_quality(self):
        energy = pd.DataFrame({"timestamp_utc": pd.to_datetime(["2025-01-01T00:00Z", "2025-01-01T02:00Z"]),
                               "load_mean_mw": [40000., -1.], "covered_seconds": [3600, 3600]})
        self.store.upsert("energy", energy, ["timestamp_utc"], "r")
        out = build_mart(self.store, {}, "r")
        self.assertEqual(len(out), 3)
        self.assertEqual(out.load_mean_mw.isna().sum(), 2)
        self.assertEqual(len(self.store.query("SELECT * FROM quality")), 8)


class LeakageTests(unittest.TestCase):
    def history(self):
        idx = pd.date_range("2025-01-01", periods=24 * 60, freq="h", tz="UTC")
        return pd.DataFrame({"load_mean_mw": 45000 + np.arange(len(idx)), "temperature_c": 5.,
                             "wind_speed_ms": 3., "solar_energy_wh_m2": 100.}, index=idx)

    def test_future_weather_issue_is_not_used(self):
        history = self.history()
        origin = history.index.max() + pd.Timedelta(hours=1)
        idx = pd.date_range(origin, periods=24, freq="h")
        archive = pd.DataFrame({"station_id": ["1", "1"], "valid_time_utc": [idx[0], idx[0]],
            "issue_time_utc": [origin - pd.Timedelta(hours=6), origin + pd.Timedelta(hours=6)],
            "temperature_c": [8., 99.], "wind_speed_ms": [2., 50.], "solar_energy_wh_m2": [20., 900.]})
        out = weather_for(history, idx, archive, origin)
        self.assertEqual(out.temperature_c.iloc[0], 8)
        self.assertEqual(out.temperature_c.iloc[1], 5)
        self.assertEqual(out.weather_source.iloc[1], "seasonal_profile")

    def test_weekly_lags_available_for_full_horizon(self):
        h = self.history()
        idx = pd.date_range(h.index.max() + pd.Timedelta(hours=1), periods=168, freq="h")
        w = weather_for(h, idx, pd.DataFrame(), idx[0])
        x = features(h, idx, w, None)
        self.assertEqual(x.lag_168.iloc[-1], h.load_mean_mw.iloc[-1])
        self.assertEqual(x.lag_336.iloc[-1], h.load_mean_mw.iloc[-169])
        self.assertFalse(x.isna().any().any())

    def test_metrics_ignore_missing_actuals(self):
        result = metrics([100, 200, np.nan], [110, 180, 10000])
        self.assertEqual(result["mae_mw"], 15)
        self.assertEqual(result["n"], 2)
        self.assertEqual(result["mape_pct"], 10)


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        cfg = load_config()
        cfg["data_dir"] = Path(self.temp.name)
        self.app = create_app(cfg)
        self.client = self.app.test_client()

    def tearDown(self):
        self.temp.cleanup()

    def test_status_and_static_site_without_data(self):
        with self.client.get("/") as response:
            self.assertEqual(response.status_code, 200)
        self.assertFalse(self.client.get("/api/status").json["ready"])
        self.assertEqual(self.client.get("/api/history").json["data"], [])

    def test_invalid_queries_return_clear_errors(self):
        for url in ("/api/forecast?hours=999", "/api/forecast?model=bad", "/api/forecast?temperature_delta=nan", "/api/history?days=-1"):
            self.assertEqual(self.client.get(url).status_code, 400)

    def test_local_write_protection(self):
        self.assertEqual(self.client.post("/api/jobs/refresh", json={}, headers={"Origin": "https://evil.example"}).status_code, 403)
        self.assertEqual(self.client.post("/api/jobs/refresh", data="").status_code, 415)
        self.assertEqual(self.client.get("/api/status", headers={"Host": "evil.example"}).status_code, 403)

    def test_job_conflict(self):
        engine = self.app.config["ENGINE"]
        engine.lock.acquire()
        try:
            self.assertEqual(self.client.post("/api/jobs/train", json={}).status_code, 409)
        finally:
            engine.lock.release()


if __name__ == "__main__":
    unittest.main()
