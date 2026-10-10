import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from gridpulse.config import load_config
from gridpulse.scenario import apply_scenario, validate_scenario
from gridpulse.service import create_app


class ScenarioTests(unittest.TestCase):
    def test_extremes_and_physical_bounds_preserve_source(self):
        source = pd.DataFrame({"temperature_c": [5., 10.], "wind_speed_ms": [2., 40.],
                               "solar_energy_wh_m2": [0., 200.]})
        result = apply_scenario(source, -40, -30, 300)
        self.assertEqual(result.temperature_c.tolist(), [-35., -30.])
        self.assertEqual(result.wind_speed_ms.tolist(), [0., 10.])
        self.assertEqual(result.solar_energy_wh_m2.tolist(), [0., 800.])
        self.assertEqual(source.temperature_c.tolist(), [5., 10.])
        pd.testing.assert_frame_equal(apply_scenario(source), source)
        self.assertEqual(apply_scenario(source, 40, 30, -100).solar_energy_wh_m2.sum(), 0)

    def test_invalid_and_nonfinite_values(self):
        for position, outside in enumerate((41, 31, 301)):
            for value in (outside, -outside - 100, float('nan'), float('inf'), -float('inf')):
                values = [0, 0, 0]
                values[position] = value
                with self.subTest(values=values), self.assertRaises(ValueError):
                    validate_scenario(*values)

    def test_api_and_csv_share_scenario_parameters(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = load_config()
            cfg['data_dir'] = Path(directory)
            app = create_app(cfg)
            client = app.test_client()
            index = pd.date_range('2026-10-10', periods=2, freq='h', tz='UTC', name='timestamp_utc')
            frame = pd.DataFrame({'prediction_mw': [100., 110.], 'temperature_c': [45., 50.],
                                  'wind_speed_ms': [0., 10.], 'solar_energy_wh_m2': [0., 800.],
                                  'weather_source': ['DWD MOSMIX'] * 2}, index=index)
            meta = dict(model='boosting', issued_at=index[0].isoformat(), model_sha256='test',
                        temperature_delta=40, wind_delta=-30, solar_delta_pct=300)
            with patch.object(app.config['ENGINE'], 'forecast', return_value=(frame, meta)) as forecast:
                query = '?hours=72&temperature_delta=40&wind_delta=-30&solar_delta_pct=300'
                response = client.get('/api/forecast' + query)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json['meta']['wind_delta'], -30)
                forecast.assert_called_with(72, 'boosting', 40., -30., 300.)
                export = client.get('/api/export/forecast' + query)
                self.assertEqual(export.status_code, 200)
                csv = pd.read_csv(io.BytesIO(export.data))
                for key in ('temperature_delta', 'wind_delta', 'solar_delta_pct'):
                    self.assertTrue(csv[key].eq(meta[key]).all())
                for endpoint in ('/api/forecast', '/api/export/forecast'):
                    for bad in ('wind_delta=nan', 'solar_delta_pct=inf', 'temperature_delta=41',
                                'wind_delta=-31', 'solar_delta_pct=-101', 'solar_delta_pct=301'):
                        self.assertEqual(client.get(endpoint + '?' + bad).status_code, 400)
