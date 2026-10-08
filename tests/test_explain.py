import unittest

import pandas as pd

from gridpulse.explain import explain_forecast


class InteractionModel:
    def predict(self, x):
        # Temperature/wind interaction should be split equally by Shapley.
        return 100 + x.temperature_c * x.wind_speed_ms + 5 * x.weekday


class ExplanationTests(unittest.TestCase):
    def setUp(self):
        self.index = pd.date_range("2025-10-25T22:00Z", periods=25, freq="h")
        self.reference = pd.DataFrame({
            "temperature_c": 0., "heating_degree": 18., "cooling_degree": 0.,
            "wind_speed_ms": 0., "solar_energy_wh_m2": 0.,
            "weekday": 1., "holiday": 0., "workday_fraction": 1.,
            "lag_168": 150.,
        }, index=self.index)
        self.actual = self.reference.copy()
        self.actual["temperature_c"] = 2.
        self.actual["heating_degree"] = 16.
        self.actual["wind_speed_ms"] = 3.
        self.actual["weekday"] = 6.
        self.actual["workday_fraction"] = 0.

    def test_interactions_efficiency_dummy_and_dst(self):
        result = explain_forecast(InteractionModel(), self.actual, self.reference, "boosting")
        energy = InteractionModel().predict(self.actual).sum()
        self.assertAlmostEqual(result["reference_energy_mwh"] + sum(result["contribution_mwh"].values()), energy)
        for key, expected in {"temperature": 3., "wind": 3., "solar": 0., "calendar": 25.}.items():
            self.assertAlmostEqual(result["contribution_mwh"][key], expected * 25)
            self.assertAlmostEqual(result["contribution_pct"][key], expected / 105 * 100)
        self.assertEqual(result["days"]["2025-10-26"]["hours"], 25)
        self.assertEqual(len(result["days"]), 1)

    def test_reference_has_zero_contributions(self):
        result = explain_forecast(InteractionModel(), self.reference, self.reference, "boosting")
        self.assertTrue(all(value == 0 for value in result["contribution_pct"].values()))

    def test_weekly_profile_does_not_use_external_features(self):
        result = explain_forecast(None, self.actual, self.reference, "seasonal")
        self.assertEqual(result["reference_energy_mwh"], 150 * 25)
        self.assertTrue(all(value == 0 for value in result["contribution_pct"].values()))

    def test_zero_reference_avoids_undefined_percentages(self):
        class ZeroReferenceModel:
            def predict(self, x):
                return x.temperature_c.to_numpy()
        result = explain_forecast(ZeroReferenceModel(), self.actual, self.reference, "boosting")
        self.assertTrue(all(value is None for value in result["contribution_pct"].values()))
        self.assertAlmostEqual(result["contribution_mwh"]["temperature"], 50.)

    def test_clipping_and_signed_contributions(self):
        class NegativeModel:
            def predict(self, x):
                return 10 - 10 * x.temperature_c
        result = explain_forecast(NegativeModel(), self.actual, self.reference, "boosting")
        self.assertAlmostEqual(result["contribution_pct"]["temperature"], -100.)
        self.assertAlmostEqual(result["reference_energy_mwh"] + sum(result["contribution_mwh"].values()), 0.)
