"""Time-ordered validation and reproducible forecasts without future target/weather leakage."""
import json
import pickle

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.metrics import mean_absolute_error, mean_squared_error
from threadpoolctl import threadpool_limits

from .calendar import calendar_days
from .data import WEATHER
from .storage import atomic_write, digest, now

VERSION = "hgb-weather-calendar-v1"
LABELS = {"boosting": "Градиентный бустинг", "seasonal": "Недельный профиль"}


def calendar_for(index, cal=None):
    local = index.tz_convert("Europe/Berlin")
    fallback = calendar_days(local.min().date(), local.max().date()).set_index("date_local")
    if cal is not None and not cal.empty:
        fallback = cal.set_index("date_local").combine_first(fallback)
    return fallback.reindex(local.strftime("%Y-%m-%d")).set_axis(index)


def lag_values(history, index, lag):
    series = history.load_mean_mw.dropna()
    values = series.reindex(index - pd.Timedelta(hours=lag)).to_numpy()
    # Gaps in measured load are filled only from available past local-hour profiles.
    local = series.index.tz_convert("Europe/Berlin")
    profile = series.groupby([local.dayofweek, local.hour]).median()
    future = index.tz_convert("Europe/Berlin")
    fallback = np.array([profile.get((d, h), series.median()) for d, h in zip(future.dayofweek, future.hour)])
    return np.where(np.isfinite(values), values, fallback)


def weather_for(history, index, archived, as_of):
    local = history.index.tz_convert("Europe/Berlin")
    climate = history[WEATHER].groupby([local.month, local.hour]).median()
    target = index.tz_convert("Europe/Berlin")
    values = pd.DataFrame(index=index)
    for col in WEATHER:
        values[col] = [climate[col].get((m, h), history[col].median()) for m, h in zip(target.month, target.hour)]
    values["weather_source"] = "seasonal_profile"
    values["weather_issue"] = None
    values["weather_stations"] = 0
    if archived is not None and not archived.empty:
        archive = archived.copy()
        for col in ("valid_time_utc", "issue_time_utc"):
            archive[col] = pd.to_datetime(archive[col], utc=True)
        archive = archive.loc[archive.issue_time_utc.le(as_of) & archive.valid_time_utc.isin(index)]
        archive = archive.sort_values("issue_time_utc").drop_duplicates(["station_id", "valid_time_utc"], keep="last")
        if not archive.empty:
            grouped = archive.groupby("valid_time_utc")
            meteo = grouped[WEATHER].mean()
            values.update(meteo)
            valid = values.index.intersection(meteo.dropna(subset=["temperature_c"]).index)
            values.loc[valid, "weather_source"] = "DWD MOSMIX"
            values.loc[valid, "weather_issue"] = grouped.issue_time_utc.max().reindex(valid).astype(str).values
            values.loc[valid, "weather_stations"] = grouped.station_id.nunique().reindex(valid).values
    return values


def features(history, index, weather, cal):
    local = index.tz_convert("Europe/Berlin")
    c = calendar_for(index, cal)
    x = pd.DataFrame(index=index)
    x["hour"] = local.hour
    x["weekday"] = local.dayofweek
    x["month"] = local.month
    x["day_sin"] = np.sin(2 * np.pi * local.dayofyear / 365.25)
    x["day_cos"] = np.cos(2 * np.pi * local.dayofyear / 365.25)
    x["hour_sin"] = np.sin(2 * np.pi * local.hour / 24)
    x["hour_cos"] = np.cos(2 * np.pi * local.hour / 24)
    x["holiday"] = pd.to_numeric(c.is_national_holiday)
    x["workday_fraction"] = pd.to_numeric(c.workday_fraction)
    x["lag_168"] = lag_values(history, index, 168)
    x["lag_336"] = lag_values(history, index, 336)
    for col in WEATHER:
        x[col] = weather[col].reindex(index).to_numpy()
    x["heating_degree"] = (18 - x.temperature_c).clip(lower=0)
    x["cooling_degree"] = (x.temperature_c - 22).clip(lower=0)
    return x.astype(float)


def metrics(actual, predicted):
    y, p = np.asarray(actual), np.asarray(predicted)
    good = np.isfinite(y) & np.isfinite(p)
    y, p = y[good], p[good]
    if not len(y):
        raise ValueError("В проверочном периоде нет фактических наблюдений")
    nonzero = np.abs(y) > 1
    return {"mae_mw": float(mean_absolute_error(y, p)),
            "rmse_mw": float(np.sqrt(mean_squared_error(y, p))),
            "mape_pct": float(np.mean(np.abs((y[nonzero] - p[nonzero]) / y[nonzero])) * 100), "n": len(y)}


def train(store, cfg, frame, run_id, progress=lambda msg: None):
    f = frame.loc[:frame.load_mean_mw.last_valid_index()].tail(cfg["train_days"] * 24).copy()
    if f.load_mean_mw.notna().sum() < 24 * 60:
        raise ValueError("Для обучения и временной проверки нужно не менее 60 суток фактической нагрузки")
    val_hours = cfg["validation_days"] * 24
    if val_hours < 336 or val_hours % 336:
        raise ValueError("validation_days должен быть кратен 14 и не меньше 14")
    cutoff = f.index[-val_hours]
    past = f.loc[f.index < cutoff]
    cal = pd.read_csv(store.root / "gold/calendar.csv")
    archive = store.frame("weather_forecast")
    x = features(past, past.index, past[WEATHER], cal)
    eligible = past.load_mean_mw.notna() & (past.index >= past.index.min() + pd.Timedelta(days=14))
    model = HistGradientBoostingRegressor(max_iter=180, learning_rate=.07, max_leaf_nodes=24,
              l2_regularization=10, early_stopping=False, random_state=cfg["random_seed"])
    progress("Обучение модели на истории до проверочного периода")
    with threadpool_limits(limits=2):
        model.fit(x.loc[eligible], past.loc[eligible, "load_mean_mw"])
    validation = []
    for offset in range(0, val_hours, 168):
        idx = f.index[-val_hours + offset:][:168]
        history = f.loc[f.index < idx.min()]
        w = weather_for(history, idx, archive, idx.min())
        vx = features(history, idx, w, cal)
        with threadpool_limits(limits=2):
            pred = model.predict(vx)
        for i, stamp in enumerate(idx):
            validation.append({"timestamp_utc": stamp.isoformat(), "origin": idx.min().isoformat(),
                "lead_hour": i + 1, "actual": f.loc[stamp, "load_mean_mw"], "boosting": max(0, float(pred[i])),
                "seasonal": float(vx.lag_168.iloc[i]), "weather_source": w.weather_source.iloc[i]})
    v = pd.DataFrame(validation)
    mid = len(v) // 2
    calibration, test = v.iloc[:mid], v.iloc[mid:]
    report = {"version": VERSION, "created_at": now(), "train_start": str(past.index.min()),
              "train_end": str(past.index.max()), "test_start": test.timestamp_utc.iloc[0],
              "test_end": test.timestamp_utc.iloc[-1], "calibration_hours": len(calibration),
              "validation_scheme": "Fixed training cutoff, weekly origins; calibration first half, test second half",
              "weather_note": "Только прогнозы погоды, выпущенные до начала окна; иначе сезонный профиль по прошлому. Фактическая будущая погода не используется.",
              "weather_forecast_fraction": float(test.weather_source.eq("DWD MOSMIX").mean()),
              "models": {}, "horizons": [], "features": list(x.columns)}
    intervals = {}
    for name in LABELS:
        residuals = (calibration.actual - calibration[name]).abs().dropna().to_numpy()
        if len(residuals) < 24:
            raise ValueError("Недостаточно фактов для калибровки интервала")
        q = float(np.quantile(residuals, min(1, np.ceil((len(residuals) + 1) * .9) / len(residuals)), method="higher"))
        intervals[name] = q
        result = metrics(test.actual, test[name])
        good = test.actual.notna()
        result["coverage_90_pct"] = float(((test.actual - test[name]).abs().loc[good] <= q).mean() * 100)
        result["interval_halfwidth_mw"] = q
        report["models"][name] = result
        for left, right in ((1, 24), (25, 72), (73, 168)):
            subset = test.loc[test.lead_hour.between(left, right)]
            report["horizons"].append({"model": name, "from": left, "to": right, **metrics(subset.actual, subset[name])})
    report["recommended_model"] = min(report["models"], key=lambda name: report["models"][name]["mae_mw"])
    progress("Обучение итоговой модели на всех доступных наблюдениях")
    x = features(f, f.index, f[WEATHER], cal)
    eligible = f.load_mean_mw.notna() & (f.index >= f.index.min() + pd.Timedelta(days=14))
    with threadpool_limits(limits=2):
        model.fit(x.loc[eligible], f.loc[eligible, "load_mean_mw"])
    model_bytes = pickle.dumps({"model": model, "intervals": intervals, "version": VERSION}, protocol=5)
    sha = digest(model_bytes)
    report["model_sha256"] = sha
    report["production_train_end"] = str(f.index.max())
    report["training_rows"] = int(eligible.sum())
    report["train_days"] = cfg["train_days"]
    report["input_sha256"] = digest((store.root / "gold/hourly.csv").read_bytes())
    atomic_write(store.root / "models" / (sha + ".pkl"), model_bytes)
    atomic_write(store.root / "models/latest.json", json.dumps(report, ensure_ascii=False, indent=2).encode())
    atomic_write(store.root / "models" / (sha + ".json"), json.dumps(report, ensure_ascii=False, indent=2).encode())
    atomic_write(store.root / "gold/validation.csv", v.to_csv(index=False).encode("utf-8-sig"))
    store.log(run_id, "model", "train", "ok", f"{int(eligible.sum())} rows; model={sha}")
    return report


def predict(store, frame, hours=24, model_name="boosting", temperature_delta=0, as_of=None):
    as_of = pd.Timestamp(as_of or now()).tz_convert("UTC")
    report = json.loads((store.root / "models/latest.json").read_text(encoding="utf-8"))
    raw = (store.root / "models" / (report["model_sha256"] + ".pkl")).read_bytes()
    if digest(raw) != report["model_sha256"]:
        raise ValueError("Контрольная сумма модели не совпала; переобучите модель")
    bundle = pickle.loads(raw)  # Only local artifacts created by train(); never uploaded by users.
    history = frame.loc[frame.index < as_of].tail(report.get("train_days", 1095) * 24)
    idx = pd.date_range(as_of.ceil("h"), periods=hours, freq="h")
    cal = pd.read_csv(store.root / "gold/calendar.csv")
    weather = weather_for(history, idx, store.frame("weather_forecast"), as_of)
    weather["temperature_c"] += temperature_delta
    x = features(history, idx, weather, cal)
    with threadpool_limits(limits=2):
        predicted = bundle["model"].predict(x) if model_name == "boosting" else x.lag_168.to_numpy()
    q = bundle["intervals"][model_name]
    c = calendar_for(idx, cal)
    output = weather.copy()
    output["prediction_mw"] = np.maximum(0, predicted)
    output["lower_mw"] = np.maximum(0, predicted - q)
    output["upper_mw"] = np.maximum(0, predicted + q)
    output["baseline_mw"] = x.lag_168
    output["workday_fraction"] = c.workday_fraction
    output["holiday_name"] = c.holiday_name.fillna("")
    output["datetime_local"] = idx.tz_convert("Europe/Berlin").astype(str)
    output.index.name = "timestamp_utc"
    latest = history.load_mean_mw.last_valid_index()
    warnings = []
    if latest > pd.Timestamp(report["production_train_end"]):
        warnings.append("Данные новее обученной модели. Дождитесь переобучения для согласованной версии прогноза.")
    age = (as_of - latest).total_seconds() / 3600
    if age > 48:
        warnings.append(f"Последнему измерению {age:.0f} ч. Обновите источники; точность может снизиться.")
    missing_weather = output.weather_source.ne("DWD MOSMIX").sum()
    if missing_weather:
        warnings.append(f"Для {missing_weather} ч нет доступного прогноза DWD: используется сезонный погодный профиль.")
    if output.weather_stations.lt(7).any():
        warnings.append("Покрытие погоды менее 7 станций в части часов; среднее рассчитано по доступным станциям.")
    if temperature_delta:
        warnings.append("Температурный сценарий показывает чувствительность модели, а не причинный эффект.")
    if model_name == "seasonal" and temperature_delta:
        warnings.append("Недельный профиль не зависит от погодного сценария.")
    warnings.append("90% — номинальный интервал, откалиброванный по прошлым ошибкам; фактическое покрытие смотрите в разделе моделей.")
    return output, {"model": model_name, "model_label": LABELS[model_name], "issued_at": as_of.isoformat(),
                    "last_actual": latest.isoformat(), "model_sha256": report["model_sha256"], "warnings": warnings,
                    "temperature_delta": temperature_delta, "metrics": report["models"][model_name]}
