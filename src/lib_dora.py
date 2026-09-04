"""Small shared helpers for the DORA metric collection scripts."""

import csv
import os
import statistics
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(__file__).parent.parent / "data"


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(
            f"Missing required environment variable {name}. Set it in your environment "
            f"(shell profile or .env file) before running this script."
        )
    return value


def mean(values: list[float]) -> float:
    return statistics.fmean(values)


def median(values: list[float]) -> float:
    return statistics.median(values)


def to_iso(dt: datetime) -> str:
    """Format like JS's Date#toISOString(): always millisecond-precision UTC
    with a Z suffix, so every row in a date column has the same width —
    Python's datetime.isoformat() omits the fraction entirely when it's zero,
    which produces a mixed-width column that defeats pandas' date parsing."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def write_csv(path: Path, header: list[str], rows: list[list]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(rows)
