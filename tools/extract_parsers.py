"""One-time extraction used during development; the service does not execute notebooks."""
import ast
import json
from pathlib import Path

notebook = json.loads(Path("Germany_Energy_Data_Loading.ipynb").read_text(encoding="utf-8"))
names = {"parse_energy", "parse_cdc", "parse_mosmix", "hourly_load", "calendar_frames", "parse_station_catalog", "distance_km"}
constants = {"NS", "STATES", "HOLIDAY_OVERRIDES", "CITIES", "VARIABLES", "CDC", "MOSMIX_BASE", "DEFINITIONS_URL", "CALENDAR_API", "ENERGY_API"}
parts = ['"""Source parsers adapted from the supplied data-loading notebook."""',
         "import io, json, re, zipfile", "from pathlib import Path", "import xml.etree.ElementTree as ET",
         "import numpy as np", "import pandas as pd"]
for cell in notebook["cells"]:
    if cell["cell_type"] != "code":
        continue
    source = "".join(cell["source"])
    for node in ast.parse(source).body:
        if (isinstance(node, ast.FunctionDef) and node.name in names) or (
            isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in constants for t in node.targets)):
            parts.append(ast.get_source_segment(source, node))
text = "\n\n".join(parts) + "\n"
text = text.replace("def parse_cdc(body, variable, station_id, digest, url):",
                    "def parse_cdc(body, variable, station_id, digest, url, start, end):")
text = text.replace("frame = in_period(frame)",
                    "frame = frame.loc[frame.timestamp_utc.ge(start) & frame.timestamp_utc.lt(end)].copy()")
Path("gridpulse/parsers.py").write_text(text, encoding="utf-8")
