"""Weather adjustments shared by API validation and forecasting."""
import math


LIMITS = {"temperature_delta": (-40, 40), "wind_delta": (-30, 30), "solar_delta_pct": (-100, 300)}


def validate_scenario(temperature_delta=0, wind_delta=0, solar_delta_pct=0):
    values = (temperature_delta, wind_delta, solar_delta_pct)
    for (name, (low, high)), value in zip(LIMITS.items(), values):
        if not math.isfinite(value) or not low <= value <= high:
            raise ValueError(f"{name}: допустимо значение от {low} до {high}")
    return values


def apply_scenario(weather, temperature_delta=0, wind_delta=0, solar_delta_pct=0):
    validate_scenario(temperature_delta, wind_delta, solar_delta_pct)
    result = weather.copy()
    result["temperature_c"] += temperature_delta
    result["wind_speed_ms"] = (result.wind_speed_ms + wind_delta).clip(lower=0)
    result["solar_energy_wh_m2"] = (result.solar_energy_wh_m2 * (1 + solar_delta_pct / 100)).clip(lower=0)
    return result
