import json
import os
from contextlib import contextmanager

from .data import bootstrap, build_mart, read_mart
from .ingest import refresh
from .model import predict, train
from .storage import atomic_write


@contextmanager
def pipeline_lock(store):
    """OS lock is released after crashes and protects against a second CLI/server writer."""
    path = store.root / "pipeline.lock"
    with open(path, "a+b") as handle:
        if path.stat().st_size == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("Конвейер уже выполняется в другом процессе") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def run_pipeline(store, cfg, online=False, progress=lambda msg: None):
    with pipeline_lock(store):
        run_id = store.begin("refresh" if online else "train")
        errors = []
        try:
            progress("Подготовка локальной истории")
            bootstrap(store, cfg, run_id)
            if online:
                errors = refresh(store, cfg, run_id, progress)
            progress("Контроль качества и построение витрины")
            frame = build_mart(store, cfg, run_id)
            train(store, cfg, frame, run_id, progress)
            progress("Формирование почасового прогноза")
            forecast, meta = predict(store, frame, 168)
            atomic_write(store.root / "gold/forecast.csv", forecast.reset_index().to_csv(index=False).encode("utf-8-sig"))
            atomic_write(store.root / "gold/forecast.json", json.dumps(meta, ensure_ascii=False, indent=2).encode())
            stored = forecast.reset_index()
            stored["issued_at"] = meta["issued_at"]
            stored["model_sha256"] = meta["model_sha256"]
            store.upsert("forecast_history", stored, ["issued_at", "timestamp_utc"], run_id)
            store.finish(run_id, "partial" if errors else "ok", "\n".join(errors))
            return {"run_id": run_id, "status": "partial" if errors else "ok", "errors": errors}
        except Exception as exc:
            store.finish(run_id, "error", str(exc))
            store.log(run_id, "pipeline", "run", "error", str(exc))
            raise
