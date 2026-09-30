import io
import json
import threading
import re
from datetime import datetime

import numpy as np
import pandas as pd
from flask import Flask, Response, jsonify, request, send_from_directory

from .config import BASE
from .data import read_mart
from .model import LABELS, predict
from .pipeline import run_pipeline
from .storage import Store, now


def records(frame):
    return json.loads(frame.to_json(orient="records", date_format="iso", double_precision=4))


class Engine:
    def __init__(self, cfg):
        self.cfg, self.store = cfg, Store(cfg["data_dir"])
        self.lock = threading.Lock()
        self.state = {"running": False, "message": "Готов к работе", "error": None}
        self.cache = {}
        self.stop = threading.Event()

    def start(self, online=False):
        if not self.lock.acquire(blocking=False):
            return False
        self.state = {"running": True, "message": "Запуск конвейера", "error": None}
        def work():
            try:
                result = run_pipeline(self.store, self.cfg, online, lambda msg: self.state.update(message=msg))
                self.cache.clear()
                self.state.update(message="Обновление завершено" if not result["errors"] else "Обновлено с предупреждениями", result=result)
            except Exception as exc:
                self.state.update(error=str(exc), message="Операция завершилась ошибкой")
            finally:
                self.state["running"] = False
                self.lock.release()
        threading.Thread(target=work, daemon=True, name="gridpulse-pipeline").start()
        return True

    def forecast(self, hours, name, delta):
        manifest = self.store.root / "models/latest.json"
        if not manifest.exists():
            raise ValueError("Модель ещё не готова. Дождитесь завершения подготовки данных.")
        key = (hours, name, delta, pd.Timestamp.now(tz="UTC").floor("h").isoformat(), manifest.stat().st_mtime_ns)
        if key not in self.cache:
            value = predict(self.store, read_mart(self.store), hours, name, delta)
            self.cache = {key: value}
        return self.cache[key]

    def schedule(self):
        minutes = self.cfg["refresh_minutes"]
        if minutes <= 0:
            return
        def loop():
            while not self.stop.wait(minutes * 60):
                self.start(online=True)
        threading.Thread(target=loop, daemon=True, name="gridpulse-scheduler").start()


def create_app(cfg, initialize=False):
    app = Flask(__name__, static_folder=str(BASE / "web"), static_url_path="/static")
    app.json.ensure_ascii = False
    engine = Engine(cfg)
    app.config["ENGINE"] = engine

    @app.before_request
    def local_origin():
        # The local service is intended for loopback use. Block cross-site writes and DNS rebinding.
        host = request.host.split(":")[0].lower()
        if host not in ("localhost", "127.0.0.1", "[", "::1"):
            return jsonify(error="Допустим только локальный адрес"), 403
        if request.method == "POST":
            origin = request.headers.get("Origin")
            if origin and origin != request.host_url.rstrip("/"):
                return jsonify(error="Запрос с другого сайта запрещён"), 403
            if not request.is_json:
                return jsonify(error="Требуется application/json"), 415

    @app.after_request
    def security_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"
        return response

    @app.errorhandler(ValueError)
    def invalid(exc):
        return jsonify(error=str(exc)), 400

    @app.get("/")
    def index():
        return send_from_directory(BASE / "web", "index.html")

    @app.get("/api/status")
    def status():
        return jsonify(**engine.state, ready=(engine.store.root / "models/latest.json").exists(),
                       time=now(), refresh_minutes=cfg["refresh_minutes"])

    @app.post("/api/jobs/<kind>")
    def jobs(kind):
        if kind not in ("refresh", "train"):
            return jsonify(error="Неизвестная операция"), 404
        if not engine.start(online=kind == "refresh"):
            return jsonify(error="Операция уже выполняется"), 409
        return jsonify(accepted=True), 202

    def forecast_params():
        hours = int(request.args.get("hours", 24))
        name = request.args.get("model", "boosting")
        delta = float(request.args.get("temperature_delta", 0))
        if hours not in (24, 72, 168) or name not in LABELS or not np.isfinite(delta) or not -10 <= delta <= 10:
            raise ValueError("Допустимы горизонты 24/72/168 ч и изменение температуры от −10 до +10 °C")
        return hours, name, delta

    @app.get("/api/forecast")
    def forecast():
        f, meta = engine.forecast(*forecast_params())
        peak = f.prediction_mw.idxmax()
        return jsonify(meta=meta, data=records(f.reset_index()), summary={
            "peak_mw": float(f.prediction_mw.max()), "peak_at": peak.isoformat(),
            "minimum_mw": float(f.prediction_mw.min()), "energy_mwh": float(f.prediction_mw.sum()),
            "temperature_mean": float(f.temperature_c.mean()),
            "weather_coverage_pct": float(f.weather_source.eq("DWD MOSMIX").mean() * 100)})

    @app.get("/api/history")
    def history():
        days = int(request.args.get("days", 7))
        if not 1 <= days <= 366:
            raise ValueError("Период истории: от 1 до 366 дней")
        f = read_mart(engine.store)
        if f.empty:
            return jsonify(data=[], summary={})
        f = f.loc[f.index > f.index.max() - pd.Timedelta(days=days)]
        last = f.load_mean_mw.last_valid_index()
        return jsonify(data=records(f.reset_index()), summary={"last_actual": str(last),
            "latest_mw": float(f.loc[last, "load_mean_mw"]) if last is not None else None,
            "complete_pct": float(f.load_mean_mw.notna().mean() * 100), "hours": len(f)})

    @app.get("/api/models")
    def models():
        path = engine.store.root / "models/latest.json"
        if not path.exists():
            raise ValueError("Модель ещё не обучена")
        return jsonify(json.loads(path.read_text(encoding="utf-8")))

    @app.get("/api/operations")
    def operations():
        s = engine.store
        quality = s.query("SELECT * FROM quality WHERE run_id=(SELECT run_id FROM quality ORDER BY id DESC LIMIT 1)")
        datasets = s.query("SELECT dataset,COUNT(*) AS rows,MAX(updated_at) AS updated_at FROM records GROUP BY dataset")
        return jsonify(quality=quality, datasets=datasets,
            runs=s.query("SELECT * FROM runs ORDER BY started_at DESC LIMIT 30"),
            journal=s.query("SELECT * FROM journal ORDER BY id DESC LIMIT 80"),
            history_count=s.query("SELECT COUNT(*) AS n FROM history")[0]["n"],
            history=s.query("SELECT id,dataset,key,valid_from,valid_to,run_id FROM history ORDER BY id DESC LIMIT 20"),
            state=engine.state, refresh_minutes=cfg["refresh_minutes"])

    @app.get("/api/lineage")
    def lineage():
        energy = engine.store.frame("energy")
        if energy.empty:
            return jsonify(steps=[])
        row = energy.sort_values("timestamp_utc").iloc[-1].to_dict()
        target = request.args.get("timestamp")
        if target:
            stamp = pd.Timestamp(target)
            if stamp.tzinfo is None:
                raise ValueError("Укажите время с часовым поясом")
            match = energy.loc[pd.to_datetime(energy.timestamp_utc, utc=True).eq(stamp)]
            if match.empty:
                raise ValueError("Час не найден")
            row = match.iloc[0].to_dict()
        origin = {k: str(v) for k, v in row.items()}
        if row.get("source_file"):
            # Resolve imported hourly energy all the way back to original response hashes.
            path = cfg["import_dir"] / "processed/energy_load_intervals.csv"
            if path.exists():
                intervals = pd.read_csv(path, usecols=["timestamp_utc", "raw_sha256", "source_url"])
                t = pd.to_datetime(intervals.timestamp_utc, utc=True)
                rows = intervals.loc[t.dt.floor("h").eq(pd.Timestamp(row["timestamp_utc"]))]
                origin["upstream"] = records(rows)
        return jsonify(origin=origin, steps=[
            "Energy-Charts /public_power → ряд Load (МВт)",
            "Bronze: ответ JSON, URL, время получения, SHA-256",
            "Интервалы: МВт × длительность / 3600 → МВт·ч",
            "Silver: полный UTC-час с покрытием 3600 с; история исправлений",
            "Gold: hourly.csv + DWD + календарь Europe/Berlin",
            "Дашборд: средняя мощность часа (МВт); энергия часа численно равна в МВт·ч"])

    @app.get("/api/export/<kind>")
    def export(kind):
        if kind == "forecast":
            f, meta = engine.forecast(*forecast_params())
            f = f.reset_index().assign(model=meta["model"], issued_at=meta["issued_at"],
                                        model_sha256=meta["model_sha256"], temperature_delta=meta["temperature_delta"])
            data = f.to_csv(index=False).encode("utf-8-sig")
        elif kind in ("history", "validation"):
            filename = "hourly.csv" if kind == "history" else "validation.csv"
            path = engine.store.root / "gold" / filename
            if not path.exists():
                raise ValueError("Данные ещё не подготовлены")
            data = path.read_bytes()
        else:
            return jsonify(error="Неизвестный набор данных"), 404
        return Response(data, mimetype="text/csv; charset=utf-8", headers={"Content-Disposition": f"attachment; filename={kind}.csv"})

    @app.get("/api/raw/<sha>")
    def raw(sha):
        if not re.fullmatch(r"[a-f0-9]{64}", sha):
            raise ValueError("Ожидается SHA-256 исходного ответа")
        for directory in (engine.store.root / "bronze/objects", cfg["import_dir"] / "raw/objects"):
            if (directory / sha).is_file():
                return send_from_directory(directory, sha, as_attachment=True, download_name=sha + ".bin")
        return jsonify(error="Исходный объект не найден в локальном архиве"), 404

    if initialize:
        if not (engine.store.root / "models/latest.json").exists():
            engine.start(online=not (cfg["import_dir"] / "processed/features_hourly.csv").exists())
        engine.schedule()
    return app
