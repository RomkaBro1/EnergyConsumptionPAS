import json

import numpy as np
import pandas as pd

from .calendar import calendar_days
from .storage import atomic_write, digest

WEATHER = ["temperature_c", "wind_speed_ms", "solar_energy_wh_m2"]


def bootstrap(store, cfg, run_id):
    """Import the user's existing exports without changing any source file."""
    if store.query("SELECT 1 FROM records LIMIT 1"):
        return False
    source = cfg["import_dir"] / "processed/features_hourly.csv"
    if not source.exists():
        return False
    f = pd.read_csv(source, low_memory=False)
    f["timestamp_utc"] = pd.to_datetime(f.timestamp_utc, utc=True)
    for dataset, columns in (("energy", ["timestamp_utc", "load_mean_mw", "covered_seconds"]),
                             ("weather", ["timestamp_utc"] + [w + "_mean" for w in WEATHER])):
        rows = f[columns].copy().rename(columns={w + "_mean": w for w in WEATHER})
        rows["source_file"] = str(source)
        rows["raw_sha256"] = digest(source.read_bytes())
        rows["source_url"] = "local-import:Germany_Energy_Data_Loading.ipynb"
        store.upsert(dataset, rows, ["timestamp_utc"], run_id)
    fc = cfg["import_dir"] / "processed/weather_forecast_all_issues.csv"
    if fc.exists():
        rows = pd.read_csv(fc, dtype={"station_id": str})
        for col in ("issue_time_utc", "valid_time_utc"):
            rows[col] = pd.to_datetime(rows[col], utc=True)
        store.upsert("weather_forecast", rows, ["station_id", "issue_time_utc", "valid_time_utc"], run_id)
    # Keep a hash manifest of the imported artifact; original raw archive stays read-only.
    atomic_write(store.root / "bronze/import.json", json.dumps({
        "file": str(source), "sha256": digest(source.read_bytes()), "rows": len(f),
        "upstream_raw": str(cfg["import_dir"] / "raw"), "run_id": run_id
    }, ensure_ascii=False, indent=2).encode())
    return True


def build_mart(store, cfg, run_id):
    energy = store.frame("energy")
    if energy.empty:
        raise ValueError("Нет данных нагрузки. Запустите загрузку источников.")
    energy["timestamp_utc"] = pd.to_datetime(energy.timestamp_utc, utc=True)
    energy = energy.set_index("timestamp_utc").sort_index()
    hours = pd.date_range(energy.index.min(), energy.index.max(), freq="h")
    f = energy[["load_mean_mw", "covered_seconds"]].reindex(hours)
    f.index.name = "timestamp_utc"
    # Exclude partial or out-of-range targets; never fabricate measured energy.
    invalid = ~f.load_mean_mw.between(0, 150000) | f.covered_seconds.ne(3600)
    f.loc[invalid, "load_mean_mw"] = np.nan
    weather = store.frame("weather")
    if not weather.empty:
        weather["timestamp_utc"] = pd.to_datetime(weather.timestamp_utc, utc=True)
        f = f.join(weather.set_index("timestamp_utc")[WEATHER])
    for col, limits in zip(WEATHER, [(-60, 60), (0, 100), (0, 1600)]):
        if col not in f:
            f[col] = np.nan
        f.loc[~f[col].between(*limits), col] = np.nan
    local = f.index.tz_convert("Europe/Berlin")
    f["date_local"] = local.strftime("%Y-%m-%d")
    end = max(local.max().date(), (pd.Timestamp.now(tz="Europe/Berlin") + pd.Timedelta(days=370)).date())
    cal = calendar_days(local.min().date(), end)
    remote = store.frame("calendar")
    if not remote.empty:
        cal = pd.concat([cal, remote]).drop_duplicates("date_local", keep="last").sort_values("date_local")
    f = f.reset_index().merge(cal, on="date_local", how="left", validate="many_to_one").set_index("timestamp_utc")
    checks = []
    def check(name, value, ok, detail):
        checks.append({"name": name, "value": float(value), "status": "ok" if ok else "warning", "detail": detail})
    check("Уникальность часов UTC", f.index.duplicated().sum(), f.index.is_unique, "Первичный ключ — UTC, включая дни перехода времени")
    check("Полнота нагрузки, %", f.load_mean_mw.notna().mean() * 100, f.load_mean_mw.notna().mean() > .99, "Неполные часы и недопустимые значения исключены из обучения")
    check("Недопустимые часы нагрузки", invalid.sum(), invalid.sum() == 0, "Диапазон 0–150 000 МВт; ровно 3 600 секунд покрытия")
    for col in WEATHER:
        pct = f[col].notna().mean() * 100
        check("Полнота " + col + ", %", pct, pct > 95, "Пропуски сохраняются; модель обрабатывает их отдельно")
    check("Связность календаря, %", f.calendar_source.notna().mean() * 100, f.calendar_source.notna().all(), "Каждому локальному дню соответствует запись календаря")
    last_valid = f.load_mean_mw.last_valid_index()
    if last_valid is None:
        raise ValueError("После контроля качества не осталось допустимых полных часов нагрузки")
    age = (pd.Timestamp.now(tz="UTC") - last_valid).total_seconds() / 3600
    check("Возраст последнего факта, ч", age, age < 48, "Предупреждение после 48 часов; ранее полученная история остаётся доступной")
    store.save_quality(run_id, checks)
    atomic_write(store.root / "gold/hourly.csv", f.reset_index().to_csv(index=False).encode("utf-8-sig"))
    atomic_write(store.root / "gold/calendar.csv", cal.to_csv(index=False).encode("utf-8-sig"))
    return f


def read_mart(store):
    path = store.root / "gold/hourly.csv"
    if not path.exists():
        return pd.DataFrame()
    f = pd.read_csv(path, low_memory=False)
    f["timestamp_utc"] = pd.to_datetime(f.timestamp_utc, utc=True)
    return f.set_index("timestamp_utc").sort_index()
