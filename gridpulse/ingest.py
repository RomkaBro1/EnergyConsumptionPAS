"""Incremental HTTP ingestion with conditional cache, retry, quarantine and lineage."""
import json
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

import certifi
import pandas as pd
import truststore

from . import parsers as p
from .calendar import api_calendar
from .storage import atomic_write, digest, now


def weather_watermark(existing, field, cfg):
    start = pd.Timestamp(cfg["history_start"], tz="Europe/Berlin").tz_convert("UTC")
    if not existing.empty and field in existing:
        known = existing.loc[existing[field].notna()]
        if not known.empty:
            start = max(start, pd.to_datetime(known.timestamp_utc, utc=True).max() - pd.Timedelta(days=cfg["overlap_days"]))
    return start


class Downloader:
    def __init__(self, store, cfg, run_id):
        self.store, self.cfg, self.run_id = store, cfg, run_id
        self.last = {}
        self.context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        self.context.load_verify_locations(cafile=certifi.where())
        if cfg.get("custom_ca_file"):
            self.context.load_verify_locations(cafile=cfg["custom_ca_file"])

    def get(self, url, source, ttl=0):
        index = self.store.root / "bronze/requests" / (digest(url.encode()) + ".json")
        old = json.loads(index.read_text(encoding="utf-8")) if index.exists() else None
        if old:
            path = self.store.root / "bronze/objects" / old["sha256"]
            if not path.exists() or digest(path.read_bytes()) != old["sha256"]:
                old = None
            elif (pd.Timestamp.now(tz="UTC") - pd.Timestamp(old["checked_at"])).total_seconds() < ttl:
                self.store.log(self.run_id, source, "download", "cache", url=url, sha=old["sha256"])
                return path.read_bytes(), old["sha256"]
        headers = {"User-Agent": "GridPulse-DE/1.0 (academic local service)"}
        if old:
            if old.get("etag"):
                headers["If-None-Match"] = old["etag"]
            if old.get("modified"):
                headers["If-Modified-Since"] = old["modified"]
        for attempt in range(self.cfg["http_attempts"]):
            gap = self.cfg["energy_request_gap"] if source == "energy" else .15
            time.sleep(max(0, gap - (time.monotonic() - self.last.get(source, -1e9))))
            self.last[source] = time.monotonic()
            delay = 2 ** (attempt + 1)
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers=headers),
                                            timeout=self.cfg["http_timeout"], context=self.context) as response:
                    body = response.read()
                    if not body:
                        raise ValueError("Пустой ответ источника")
                    sha = digest(body)
                    atomic_write(self.store.root / "bronze/objects" / sha, body)
                    meta = {"url": url, "source": source, "sha256": sha, "checked_at": now(),
                            "retrieved_at": now(), "bytes": len(body), "etag": response.headers.get("ETag"),
                            "modified": response.headers.get("Last-Modified")}
                    atomic_write(index, json.dumps(meta, ensure_ascii=False).encode())
                    self.store.log(self.run_id, source, "download", "ok", f"HTTP {response.status}; {len(body)} bytes; attempt {attempt + 1}", url, sha)
                    return body, sha
            except urllib.error.HTTPError as exc:
                if exc.code == 304 and old:
                    old["checked_at"] = now()
                    atomic_write(index, json.dumps(old).encode())
                    self.store.log(self.run_id, source, "download", "not_modified", url=url, sha=old["sha256"])
                    return path.read_bytes(), old["sha256"]
                self.store.log(self.run_id, source, "download", "error", str(exc), url)
                if exc.code not in (408, 429, 500, 502, 503, 504) or attempt == self.cfg["http_attempts"] - 1:
                    raise
                retry = exc.headers.get("Retry-After")
                if retry:
                    try:
                        delay = float(retry)
                    except ValueError:
                        delay = (pd.Timestamp(parsedate_to_datetime(retry)) - pd.Timestamp.now(tz="UTC")).total_seconds()
                    if delay > 60:
                        raise RuntimeError("Источник просит повторить позже: Retry-After=" + retry) from exc
            except (urllib.error.URLError, TimeoutError, ConnectionError, ssl.SSLError) as exc:
                self.store.log(self.run_id, source, "download", "error", str(exc), url)
                if "CERTIFICATE_VERIFY_FAILED" in str(exc) or attempt == self.cfg["http_attempts"] - 1:
                    raise
            time.sleep(max(0, delay))
        raise RuntimeError("Загрузка не выполнена")


def refresh(store, cfg, run_id, progress=lambda message: None):
    dl = Downloader(store, cfg, run_id)
    errors = []
    def safe(source, url, work):
        progress(source + ": " + url.rsplit("/", 1)[-1][:70])
        try:
            work()
        except Exception as exc:
            message = f"{source}: {exc}"
            errors.append(message)
            store.log(run_id, source, "pipeline", "error", message, url)
            atomic_write(store.root / "quarantine" / (digest((run_id + url).encode()) + ".json"),
                         json.dumps({"run_id": run_id, "source": source, "url": url, "error": str(exc)}, ensure_ascii=False).encode())

    end = pd.Timestamp.now(tz="UTC").floor("h")
    existing = store.frame("energy")
    start = pd.Timestamp(cfg["history_start"], tz="Europe/Berlin").tz_convert("UTC")
    if not existing.empty:
        start = max(start, pd.to_datetime(existing.timestamp_utc, utc=True).max() - pd.Timedelta(days=cfg["overlap_days"]))
    cuts = [start] + [pd.Timestamp(f"{y}-01-01", tz="UTC") for y in range(start.year + 1, end.year + 1)] + [end]
    for left, right in zip(cuts[:-1], cuts[1:]):
        url = p.ENERGY_API + "?" + urllib.parse.urlencode({"country": "de", "start": left.isoformat(), "end": right.isoformat()})
        def energy(url=url, left=left, right=right):
            body, sha = dl.get(url, "energy", 900)
            intervals = p.parse_energy(body, sha, url, left, right)
            h = p.hourly_load(intervals)
            h["source_url"], h["raw_sha256"] = url, sha
            store.upsert("energy", h[["timestamp_utc", "load_mean_mw", "covered_seconds", "source_url", "raw_sha256"]], ["timestamp_utc"], run_id)
        safe("Energy-Charts", url, energy)

    # Stable station mapping, initially from the supplied notebook or config shipped with code.
    mapping = cfg["import_dir"] / "metadata/stations_selected.csv"
    if not mapping.exists():
        from .config import BASE
        mapping = BASE / "stations.csv"
    selected = pd.read_csv(mapping, dtype={"station_id": str})
    weather_existing = store.frame("weather")
    summaries = []
    for variable, field in (("temperature", "temperature_c"), ("wind", "wind_speed_ms"), ("solar", "solar_energy_wh_m2")):
        # Separate watermarks are essential: solar archives may arrive a month later.
        weather_start = weather_watermark(weather_existing, field, cfg)
        spec = p.VARIABLES[variable]
        dirs = [p.CDC + spec["folder"] + "/"] if variable == "solar" else [p.CDC + spec["folder"] + "/recent/"]
        if variable != "solar" and (weather_existing.empty or weather_start < end - pd.Timedelta(days=365)):
            dirs.insert(0, p.CDC + spec["folder"] + "/historical/")
        rows = []
        for directory in dirs:
            def weather_directory(directory=directory):
                html, _ = dl.get(directory, "dwd", 86400)
                links = re.findall(r'href=["\x27]([^"\x27]+)["\x27]', html.decode())
                for station in selected.loc[selected.variable.eq(variable), "station_id"].unique():
                    names = [n for n in links if re.match("stundenwerte_" + spec["code"] + "_" + station + "_", n) and n.endswith(".zip")]
                    for name in sorted(names):
                        match = re.search(r"_(\d{8})_(\d{8})_hist", name)
                        if match and match.group(2) < weather_start.strftime("%Y%m%d"):
                            continue
                        url = urllib.parse.urljoin(directory, name)
                        def archive(url=url, station=station):
                            body, sha = dl.get(url, "dwd", 30 * 86400 if "hist.zip" in url else 6 * 3600)
                            frame = p.parse_cdc(body, variable, station, sha, url, weather_start, end)
                            if not frame.empty:
                                rows.append(frame)
                        safe("DWD " + variable, url, archive)
            safe("DWD каталог", directory, weather_directory)
        if rows:
            obs = pd.concat(rows).drop_duplicates(["station_id", "timestamp_utc"], keep="last")
            obs = obs.merge(selected.loc[selected.variable.eq(variable), ["station_id", "city"]], on="station_id")
            summary = obs.groupby("timestamp_utc").agg(**{field: (field, "mean"), field + "_n": (field, "count"),
                        field + "_sha": ("raw_sha256", lambda v: "|".join(sorted(set(v)))),
                        field + "_url": ("source_url", lambda v: "|".join(sorted(set(v))))})
            summaries.append(summary)
    if summaries:
        update = pd.concat(summaries, axis=1)
        if not weather_existing.empty:
            weather_existing["timestamp_utc"] = pd.to_datetime(weather_existing.timestamp_utc, utc=True)
            # Only overwrite successfully supplied values; failures never erase earlier observations.
            old = weather_existing.set_index("timestamp_utc")
            update = update.combine_first(old)
        update.index.name = "timestamp_utc"
        store.upsert("weather", update.reset_index(), ["timestamp_utc"], run_id)

    units = {}
    def definitions():
        body, _ = dl.get(p.DEFINITIONS_URL, "mosmix", 86400)
        for element in ET.fromstring(body):
            values = {x.tag.split("}")[-1]: x.text for x in element}
            if "ShortName" in values:
                units[values["ShortName"]] = values.get("UnitOfMeasurement")
    safe("DWD единицы", p.DEFINITIONS_URL, definitions)
    if units:
        for city in p.CITIES:
            station = city["mosmix"]
            url = p.MOSMIX_BASE + f"{station}/kml/MOSMIX_L_LATEST_{station}.kmz"
            def forecast(url=url, station=station):
                body, sha = dl.get(url, "mosmix", 3600)
                frame = p.parse_mosmix(body, station, sha, url, units)
                store.upsert("weather_forecast", frame, ["station_id", "issue_time_utc", "valid_time_utc"], run_id)
            safe("DWD MOSMIX", url, forecast)

    # Old dates use a pinned offline calendar; fetch the current and following year remotely.
    for year in (end.year, end.year + 1):
        url = p.CALENDAR_API + "?" + urllib.parse.urlencode({"jahr": year})
        def calendar(url=url, year=year):
            body, sha = dl.get(url, "calendar", 7 * 86400)
            _, days = p.calendar_frames(json.loads(body), year, sha, url)
            store.upsert("calendar", api_calendar(days), ["date_local"], run_id)
        safe("Календарь", url, calendar)
    return errors
