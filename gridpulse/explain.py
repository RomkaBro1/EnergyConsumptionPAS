"""Exact baseline Shapley values for four weather/calendar groups.

History, hour and season stay fixed. This explains a contrast with a reference
scenario, not causal effects or global feature importance.
"""
from math import factorial

import numpy as np
import pandas as pd
from threadpoolctl import threadpool_limits


GROUPS = {
    "temperature": ["temperature_c", "heating_degree", "cooling_degree"],
    "wind": ["wind_speed_ms"],
    "solar": ["solar_energy_wh_m2"],
    "calendar": ["weekday", "holiday", "workday_fraction"],
}


def explain_forecast(model, actual, reference, model_name):
    """Enumerate all 16 coalitions; temperature/calendar columns move together."""
    n = len(GROUPS)
    if model_name == "seasonal":
        base = np.maximum(0, actual.lag_168.to_numpy())
        values = np.zeros((len(actual), n))
    else:
        scenarios = []
        for mask in range(1 << n):
            scenario = reference.copy()
            for j, columns in enumerate(GROUPS.values()):
                if mask & (1 << j):
                    scenario[columns] = actual[columns]
            scenarios.append(scenario)
        with threadpool_limits(limits=2):
            predictions = np.maximum(0, np.asarray(model.predict(pd.concat(scenarios))))
        predictions = predictions.reshape(1 << n, len(actual))
        base = predictions[0]
        values = np.zeros((len(actual), n))
        for j in range(n):
            for mask in range(1 << n):
                if mask & (1 << j):
                    continue
                size = mask.bit_count()
                weight = factorial(size) * factorial(n - size - 1) / factorial(n)
                values[:, j] += weight * (predictions[mask | (1 << j)] - predictions[mask])

    def summarize(selected):
        energy = float(base[selected].sum())
        contributions = values[selected].sum(axis=0)
        return {"hours": int(np.count_nonzero(selected)), "reference_energy_mwh": energy,
                "contribution_mwh": dict(zip(GROUPS, map(float, contributions))),
                "contribution_pct": {key: float(value / energy * 100) if energy > 1e-9 else None
                                     for key, value in zip(GROUPS, contributions)}}

    dates = actual.index.tz_convert("Europe/Berlin").strftime("%Y-%m-%d")
    return {"method": "grouped_baseline_shapley_v1", "model": model_name,
            "reference": "seasonal_weather_ordinary_tuesday",
            **summarize(np.ones(len(actual), dtype=bool)),
            "days": {day: summarize(dates == day) for day in dict.fromkeys(dates)}}
