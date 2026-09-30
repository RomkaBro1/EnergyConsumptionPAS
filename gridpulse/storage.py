"""Bronze content-addressed files, versioned silver records, durable run journal."""
import hashlib
import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temp.write_bytes(data)
    os.replace(temp, path)


class Store:
    def __init__(self, root):
        self.root = Path(root)
        for folder in ("bronze/objects", "bronze/requests", "gold", "models", "quarantine"):
            (self.root / folder).mkdir(parents=True, exist_ok=True)
        self.path = self.root / "silver.sqlite"
        with self.connect() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS records (
                  dataset TEXT NOT NULL, key TEXT NOT NULL, payload TEXT NOT NULL,
                  hash TEXT NOT NULL, updated_at TEXT NOT NULL, run_id TEXT NOT NULL,
                  PRIMARY KEY(dataset,key));
                CREATE TABLE IF NOT EXISTS history (
                  id INTEGER PRIMARY KEY, dataset TEXT, key TEXT, payload TEXT,
                  hash TEXT, valid_from TEXT, valid_to TEXT, run_id TEXT);
                CREATE TABLE IF NOT EXISTS runs (
                  id TEXT PRIMARY KEY, kind TEXT, started_at TEXT, finished_at TEXT,
                  status TEXT, message TEXT);
                CREATE TABLE IF NOT EXISTS journal (
                  id INTEGER PRIMARY KEY, run_id TEXT, at TEXT, source TEXT,
                  stage TEXT, status TEXT, message TEXT, url TEXT, sha256 TEXT);
                CREATE TABLE IF NOT EXISTS quality (
                  id INTEGER PRIMARY KEY, run_id TEXT, at TEXT, name TEXT,
                  status TEXT, value REAL, detail TEXT);
            ''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=60)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def query(self, sql, params=()):
        with self.connect() as db:
            return [dict(r) for r in db.execute(sql, params)]

    def begin(self, kind):
        run_id = uuid.uuid4().hex
        with self.connect() as db:
            db.execute("INSERT INTO runs VALUES (?,?,?,NULL,'running','')", (run_id, kind, now()))
        return run_id

    def finish(self, run_id, status, message=""):
        with self.connect() as db:
            db.execute("UPDATE runs SET finished_at=?,status=?,message=? WHERE id=?",
                       (now(), status, message, run_id))

    def log(self, run_id, source, stage, status, message="", url="", sha=""):
        with self.connect() as db:
            db.execute("INSERT INTO journal VALUES(NULL,?,?,?,?,?,?,?,?)",
                       (run_id, now(), source, stage, status, str(message), url, sha))

    def upsert(self, dataset, frame, keys, run_id):
        if frame.empty:
            return {"new": 0, "changed": 0, "unchanged": 0}
        if frame[keys].isna().any().any() or frame.duplicated(keys).any():
            raise ValueError(dataset + ": пустые или повторяющиеся ключи")
        # pandas provides a consistent, JSON-safe conversion including NaN and numpy scalars.
        rows = json.loads(frame.to_json(orient="records", date_format="iso", double_precision=12))
        counts = {"new": 0, "changed": 0, "unchanged": 0}
        stamp = now()
        with self.connect() as db:
            old = {r["key"]: r for r in db.execute("SELECT * FROM records WHERE dataset=?", (dataset,))}
            changes, history = [], []
            for row in rows:
                key = json.dumps([row[k] for k in keys], ensure_ascii=False)
                payload = json.dumps(row, sort_keys=True, ensure_ascii=False)
                business = {k: v for k, v in row.items() if k not in ("raw_sha256", "source_url", "source_file")}
                h = digest(json.dumps(business, sort_keys=True).encode())
                prev = old.get(key)
                if prev and prev["hash"] == h:
                    counts["unchanged"] += 1
                    continue
                counts["changed" if prev else "new"] += 1
                if prev:
                    history.append((dataset, key, prev["payload"], prev["hash"], prev["updated_at"], stamp, run_id))
                changes.append((dataset, key, payload, h, stamp, run_id))
            db.executemany("INSERT INTO history VALUES(NULL,?,?,?,?,?,?,?)", history)
            db.executemany('''INSERT INTO records VALUES(?,?,?,?,?,?) ON CONFLICT(dataset,key)
                DO UPDATE SET payload=excluded.payload,hash=excluded.hash,
                updated_at=excluded.updated_at,run_id=excluded.run_id''', changes)
        self.log(run_id, dataset, "normalize", "ok", json.dumps(counts))
        return counts

    def frame(self, dataset):
        with self.connect() as db:
            return pd.DataFrame([json.loads(r[0]) for r in db.execute(
                "SELECT payload FROM records WHERE dataset=? ORDER BY key", (dataset,))])

    def save_quality(self, run_id, checks):
        with self.connect() as db:
            db.executemany("INSERT INTO quality VALUES(NULL,?,?,?,?,?,?)",
                           [(run_id, now(), c["name"], c["status"], c["value"], c["detail"]) for c in checks])
