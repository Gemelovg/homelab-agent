"""Append-only JSONL audit log of every tool call the model makes."""

import functools
import json
import time
from datetime import UTC, datetime
from pathlib import Path

_path: Path = Path("audit.jsonl")


def configure(path: Path) -> None:
    global _path
    _path = path


def _write(record: dict) -> None:
    with _path.open("a") as f:
        f.write(json.dumps(record, default=str) + "\n")


def audited(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        start = time.monotonic()
        record = {"ts": datetime.now(UTC).isoformat(), "tool": fn.__name__, "args": kwargs}
        try:
            result = fn(*args, **kwargs)
            record["ok"] = True
            return result
        except Exception as e:
            record.update(ok=False, error=f"{type(e).__name__}: {e}")
            raise
        finally:
            record["duration_ms"] = round((time.monotonic() - start) * 1000)
            _write(record)

    return wrapper
