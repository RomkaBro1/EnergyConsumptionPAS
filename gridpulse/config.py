import json
import os
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]


def load_config(path=None):
    cfg = json.loads(Path(path or BASE / "config.json").read_text(encoding="utf-8"))
    for key in ("data_dir", "import_dir", "host", "port", "custom_ca_file", "refresh_minutes"):
        env = os.getenv("GRIDPULSE_" + key.upper())
        if env is not None:
            cfg[key] = int(env) if key in ("port", "refresh_minutes") else env
    for key in ("data_dir", "import_dir"):
        cfg[key] = (BASE / cfg[key]).resolve()
    return cfg
