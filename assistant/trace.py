"""JSON-lines trace log. Callers must never pass phone, DOB or keys in here."""

import json
import time
from pathlib import Path

LOG_FILE = Path(__file__).resolve().parent.parent / "logs" / "trace.jsonl"


def trace(event, **fields):
    LOG_FILE.parent.mkdir(exist_ok=True)
    line = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": event, **fields}
    with open(LOG_FILE, "a") as fh:
        fh.write(json.dumps(line) + "\n")
