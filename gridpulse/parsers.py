"""Source parsers adapted from the supplied data-loading notebook."""

import io, json, re, zipfile

from pathlib import Path

import xml.etree.ElementTree as ET

import numpy as np

import pandas as pd

ENERGY_API = "https://api.energy-charts.info/public_power"

def parse_energy(body, digest, url, start, end):
    obj = json.loads(body)
    if obj.get("deprecated"): raise ValueError("API v1 deprecated: требуется адаптация схемы")
    ts = pd.to_datetime(obj["unix_seconds"], unit="s", utc=True)
    series = [s for s in obj["production_types"] if s.get("name") == "Load"]
    if len(series) != 1: raise ValueError("Нет единственного ряда Load")
    if len(ts) != len(series[0]["data"]): raise ValueError("Длины временного ряда не совпадают")
    frame = pd.DataFrame({"timestamp_utc": ts,
                          "load_mw": pd.to_numeric(pd.Series(series[0]["data"]), errors="coerce")})
    frame = frame.loc[frame.timestamp_utc.ge(start) & frame.timestamp_utc.lt(end)].copy()
    frame = frame.sort_values("timestamp_utc")
    if frame.timestamp_utc.duplicated().any(): raise ValueError("Повтор временных меток Energy-Charts")
    if (frame.load_mw.dropna() < 0).any(): raise ValueError("Отрицательная нагрузка")
    # Формат v1 имеет шаг 15 или 60 минут; фиксируем его по регулярным соседним меткам.
    diffs = frame.timestamp_utc.diff().dropna().dt.total_seconds()
    if diffs.empty: raise ValueError("Недостаточно точек для определения шага")
    step = int(diffs.mode().iloc[0])
    if step not in [900, 3600] or (diffs % step != 0).any():
        raise ValueError(f"Неподдерживаемый шаг временного ряда: {step} с")
    frame["interval_seconds"] = step
    frame["raw_sha256"] = digest
    frame["source_url"] = url
    return frame

CDC = "https://opendata.dwd.de/climate_environment/CDC/observations_germany/climate/hourly/"

VARIABLES = {
    "temperature": {"folder":"air_temperature", "code":"TU", "catalog":"TU_Stundenwerte_Beschreibung_Stationen.txt"},
    "wind": {"folder":"wind", "code":"FF", "catalog":"FF_Stundenwerte_Beschreibung_Stationen.txt"},
    "solar": {"folder":"solar", "code":"ST", "catalog":"ST_Stundenwerte_Beschreibung_Stationen.txt"},
}

CITIES = [
    {"city":"Berlin", "lat":52.52, "lon":13.405, "mosmix":"10384"},
    {"city":"Hamburg", "lat":53.551, "lon":9.994, "mosmix":"10147"},
    {"city":"Frankfurt", "lat":50.110, "lon":8.682, "mosmix":"10637"},
    {"city":"Cologne", "lat":50.938, "lon":6.960, "mosmix":"10513"},
    {"city":"Stuttgart", "lat":48.775, "lon":9.182, "mosmix":"10738"},
    {"city":"Leipzig", "lat":51.340, "lon":12.375, "mosmix":"10469"},
    {"city":"Munich", "lat":48.137, "lon":11.576, "mosmix":"10865"},
]

def parse_station_catalog(body):
    rows = []
    for line in body.decode("latin1").splitlines():
        parts = line.split(maxsplit=6)
        if len(parts) < 7 or not re.fullmatch(r"\d{5}", parts[0]): continue
        rows.append({"station_id":parts[0], "from_date":parts[1], "to_date":parts[2],
                     "height_m":float(parts[3]), "lat":float(parts[4]), "lon":float(parts[5]),
                     "name_and_state":parts[6].strip()})
    if not rows: raise ValueError("Каталог станций пуст или схема изменилась")
    return pd.DataFrame(rows)

def distance_km(lat, lon, lat0, lon0):
    p1, p2 = np.radians(lat), np.radians(lat0)
    dlat, dlon = p1 - p2, np.radians(lon - lon0)
    a = np.sin(dlat/2)**2 + np.cos(p1)*np.cos(p2)*np.sin(dlon/2)**2
    return 6371 * 2 * np.arcsin(np.sqrt(np.clip(a, 0, 1)))

def parse_cdc(body, variable, station_id, digest, url, start, end):
    chunks = []
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        product = [n for n in archive.namelist() if Path(n).name.startswith("produkt_") and n.endswith(".txt")]
        if len(product) != 1: raise ValueError("В ZIP нет единственного produkt_*.txt")
        with archive.open(product[0]) as stream:
            for frame in pd.read_csv(stream, sep=";", encoding="latin1", dtype={"MESS_DATUM":"string", "STATIONS_ID":"string"},
                                     na_values=[-999, "-999"], skipinitialspace=True, chunksize=100000):
                frame.columns = frame.columns.str.strip()
                stamp = frame["MESS_DATUM"].str.strip()
                fmt = "%Y%m%d%H:%M" if variable == "solar" else "%Y%m%d%H"
                frame["timestamp_utc"] = pd.to_datetime(stamp, format=fmt, utc=True, errors="raise")
                frame = frame.loc[frame.timestamp_utc.ge(start) & frame.timestamp_utc.lt(end)].copy()
                if frame.empty: continue
                ids = frame["STATIONS_ID"].str.strip().str.zfill(5)
                if not ids.eq(station_id).all(): raise ValueError("ID станции не соответствует архиву")
                out = pd.DataFrame({"station_id":station_id, "timestamp_utc":frame.timestamp_utc,
                                    "source_timestamp":stamp.loc[frame.index]})
                if variable == "temperature":
                    out["temperature_c"] = pd.to_numeric(frame["TT_TU"], errors="raise")
                    out["relative_humidity_pct"] = pd.to_numeric(frame["RF_TU"], errors="raise")
                    out["quality_code"] = frame["QN_9"]
                    if ((out.relative_humidity_pct.dropna() < 0) | (out.relative_humidity_pct.dropna() > 100)).any():
                        raise ValueError("Влажность вне диапазона 0–100%")
                elif variable == "wind":
                    out["wind_speed_ms"] = pd.to_numeric(frame["F"], errors="raise")
                    out["wind_direction_deg"] = pd.to_numeric(frame["D"], errors="raise")
                    out["quality_code"] = frame["QN_3"]
                    if (out.wind_speed_ms.dropna() < 0).any(): raise ValueError("Отрицательная скорость ветра")
                else:
                    out["global_radiation_j_cm2"] = pd.to_numeric(frame["FG_LBERG"], errors="raise")
                    out["solar_energy_wh_m2"] = out.global_radiation_j_cm2 * 10000 / 3600
                    out["sunshine_minutes"] = pd.to_numeric(frame["SD_LBERG"], errors="raise")
                    out["quality_code"] = frame["QN_592"]
                    out["timestamp_true_solar"] = frame["MESS_DATUM_WOZ"].astype("string").str.strip()
                out["raw_sha256"], out["source_url"] = digest, url
                chunks.append(out)
    if not chunks: return pd.DataFrame()
    frame = pd.concat(chunks, ignore_index=True).sort_values("timestamp_utc")
    if frame.duplicated(["station_id", "timestamp_utc"]).any(): raise ValueError("Дубли внутри архива DWD")
    return frame

MOSMIX_BASE = "https://opendata.dwd.de/weather/local_forecasts/mos/MOSMIX_L/single_stations/"

DEFINITIONS_URL = "https://opendata.dwd.de/weather/lib/MetElementDefinition.xml"

NS = {"k":"http://www.opengis.net/kml/2.2",
      "d":"https://opendata.dwd.de/weather/lib/pointforecast_dwd_extension_V1_0.xsd"}

def parse_mosmix(body, station_id, digest, url, units):
    expected = {"TTT":"K", "Td":"K", "FF":"m/s", "Rad1h":"kJ/m2"}
    for name, unit in expected.items():
        if units.get(name) != unit: raise ValueError(f"Единица {name} изменилась: {units.get(name)}")
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        names = [n for n in archive.namelist() if n.lower().endswith(".kml")]
        if len(names) != 1: raise ValueError("KMZ должен содержать один KML")
        root = ET.fromstring(archive.read(names[0]))
    issue = pd.to_datetime(root.findtext(".//d:IssueTime", namespaces=NS), utc=True)
    times = pd.to_datetime([x.text for x in root.findall(".//d:ForecastTimeSteps/d:TimeStep", NS)], utc=True)
    place = root.find(".//k:Placemark", NS)
    if place is None or place.findtext("k:name", namespaces=NS) != station_id:
        raise ValueError("MOSMIX: ID станции не совпадает")
    lon, lat, *_ = map(float, place.findtext("k:Point/k:coordinates", namespaces=NS).split(","))
    out = pd.DataFrame({"station_id":station_id, "issue_time_utc":issue, "valid_time_utc":times,
                        "station_name":place.findtext("k:description", namespaces=NS), "lat":lat, "lon":lon})
    fields = {"TTT":"temperature_c", "Td":"dewpoint_c", "FF":"wind_speed_ms",
              "DD":"wind_direction_deg", "Rad1h":"solar_energy_wh_m2"}
    found = set()
    for element in place.findall(".//d:Forecast", NS):
        name = element.get("{" + NS["d"] + "}elementName")
        if name not in fields: continue
        raw = element.findtext("d:value", namespaces=NS).split()
        if len(raw) != len(times): raise ValueError(f"MOSMIX: длина {name} не совпадает со сроками")
        values = pd.to_numeric(pd.Series([None if x == "-" else x for x in raw]), errors="raise").to_numpy()
        if name in ["TTT", "Td"]: values = values - 273.15
        if name == "Rad1h": values = values / 3.6
        out[fields[name]] = values
        found.add(name)
    if not set(fields).issubset(found): raise ValueError(f"MOSMIX: отсутствуют {set(fields)-found}")
    out["lead_hours"] = (out.valid_time_utc - out.issue_time_utc).dt.total_seconds()/3600
    out["raw_sha256"], out["source_url"] = digest, url
    return out

CALENDAR_API = "https://feiertage-api.de/api/"

STATES = ["BW", "BY", "BE", "BB", "HB", "HH", "HE", "MV", "NI", "NW", "RP", "SL", "SN", "ST", "SH", "TH"]

HOLIDAY_OVERRIDES = {
    # ("BW", "Reformationstag"): False,  # пример; проверяйте конкретный hinweis
}

def calendar_frames(obj, year, digest, url):
    if not set(STATES + ["NATIONAL"]).issubset(obj): raise ValueError("Календарь: отсутствуют земли/NATIONAL")
    events = []
    for state in STATES + ["NATIONAL"]:
        for name, info in obj[state].items():
            date = pd.Timestamp(info["datum"]).date().isoformat()
            if pd.Timestamp(date).year != year: raise ValueError("Дата праздника вне запрошенного года")
            note = (info.get("hinweis") or "").strip()
            override = HOLIDAY_OVERRIDES.get((state, name))
            definite = state == "NATIONAL" or (override is True) or (not note and override is not False)
            conditional = bool(note) and override is None and state != "NATIONAL"
            events.append({"state":state, "date":date, "holiday_name":name, "note":note,
                           "definite":bool(definite), "conditional":conditional,
                           "manual_override":override, "raw_sha256":digest, "source_url":url})
    event_frame = pd.DataFrame(events)
    national = set(event_frame.loc[event_frame.state.eq("NATIONAL"), "date"])
    rows = []
    for day in pd.date_range(f"{year}-01-01", f"{year}-12-31", freq="D"):
        date = day.date().isoformat()
        weekend = day.dayofweek >= 5
        for state in STATES:
            day_events = event_frame.loc[event_frame.state.eq(state) & event_frame.date.eq(date)]
            definite = date in national or bool(day_events.definite.any())
            conditional = bool(day_events.conditional.any()) and not definite
            workday = False if weekend or definite else (None if conditional else True)
            rows.append({"date":date, "state":state, "weekday":int(day.dayofweek),
                         "is_weekend":bool(weekend), "is_national_holiday":date in national,
                         "is_state_holiday":definite, "has_conditional_event":conditional,
                         "is_workday_estimate":workday,
                         "holiday_names":" | ".join(day_events.holiday_name),
                         "raw_sha256":digest, "source_url":url})
    return event_frame, pd.DataFrame(rows)

def hourly_load(intervals):
    f = intervals.copy().sort_values("timestamp_utc")
    f["timestamp_utc"] = pd.to_datetime(f.timestamp_utc, utc=True)
    if f.timestamp_utc.duplicated().any(): raise ValueError("Нагрузка: повтор ключа времени")
    # Поддерживаются интервалы, полностью лежащие внутри UTC-часа.
    offsets = f.timestamp_utc.dt.minute * 60 + f.timestamp_utc.dt.second
    if (offsets + f.interval_seconds > 3600).any(): raise ValueError("Интервал пересекает границу часа")
    next_time = f.timestamp_utc.shift(-1)
    if ((f.timestamp_utc + pd.to_timedelta(f.interval_seconds, unit="s") > next_time).fillna(False)).any():
        raise ValueError("Перекрывающиеся интервалы нагрузки")
    f["timestamp_utc_hour"] = f.timestamp_utc.dt.floor("h")
    f["valid_seconds"] = np.where(f.load_mw.notna(), f.interval_seconds, 0)
    f["energy_piece_mwh"] = f.load_mw * f.interval_seconds / 3600
    h = f.groupby("timestamp_utc_hour").agg(
        covered_seconds=("valid_seconds", "sum"), intervals_received=("load_mw", "size"),
        partial_energy_mwh=("energy_piece_mwh", lambda s:s.sum(min_count=1))).reset_index()
    h = h.rename(columns={"timestamp_utc_hour":"timestamp_utc"})
    complete = h.covered_seconds.eq(3600)
    h["load_energy_mwh"] = h.partial_energy_mwh.where(complete)
    h["load_mean_mw"] = h.load_energy_mwh  # численно равно энергии за ровно один час
    h["load_hour_complete"] = complete
    return h
