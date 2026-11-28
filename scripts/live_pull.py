#!/usr/bin/env python3
"""Read the last hours of the live tape and the bot's records from S3 (liveship.py's minute files), never from the live host.

    AWS_PROFILE=moonshot .venv/bin/python scripts/live_pull.py decisions --since "2026-11-28 09:30" --until "2026-11-28 10:00" [--ticker KXXRP15M-26NOV280945] [--lane wave_shape] [--out x.csv]

Times are Eastern. Tables: quotes, flow_1s, index_ticks, index_fast, decisions, screen_log, orders. decisions, screen_log and orders can hold several copies of a row (shipped again after
a fill, a sale or a settlement): the newest copy of each id is kept. In Python: `from live_pull import read; t = read("quotes", t0, t1)` gives a pyarrow Table.
"""

from __future__ import annotations

import argparse
import io
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from kalshi_moonshot import liveship  # noqa: E402

ET = ZoneInfo("America/New_York")


def bucket_name() -> str:
    if os.environ.get("ARCHIVE_BUCKET"):
        return os.environ["ARCHIVE_BUCKET"]
    for line in (Path(__file__).resolve().parents[1] / ".env").read_text().splitlines():
        if line.startswith("ARCHIVE_BUCKET="):
            return line.split("=", 1)[1].strip().strip('"')
    raise SystemExit("ARCHIVE_BUCKET is not set")


def read(table: str, t0: float, t1: float, client=None, bucket: str | None = None):
    """The rows of `table` with a time in [t0, t1) as a pyarrow Table, newest copy per id for the mutable tables."""
    import boto3
    import pyarrow as pa
    import pyarrow.parquet as pq

    client = client or boto3.client("s3", region_name=os.environ.get("ARCHIVE_REGION", "us-east-1"))
    bucket = bucket or bucket_name()
    parts = []
    for minute in range(int(t0 // 60), int(t1 // 60) + 1):
        try:
            body = client.get_object(Bucket=bucket, Key=liveship.key_for(table, minute))["Body"].read()
        except client.exceptions.NoSuchKey:
            continue
        parts.append(pq.read_table(io.BytesIO(body)))
    if not parts:
        return pa.table({c: pa.array([], type=liveship.archive._arrow_type(k)) for c, k in liveship.LIVE_TABLES[table]})
    t = pa.concat_tables(parts)
    if "shipped_ts" in t.column_names:
        rows = sorted(t.to_pylist(), key=lambda r: r["shipped_ts"])
        newest = {r["id"]: r for r in rows}  # a later copy replaces an earlier one
        t = pa.Table.from_pylist(sorted(newest.values(), key=lambda r: r["ts"]), schema=t.schema)
    return t


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("table", choices=list(liveship.LIVE_TABLES))
    ap.add_argument("--since", required=True, help="Eastern time, YYYY-MM-DD HH:MM")
    ap.add_argument("--until", required=True)
    ap.add_argument("--ticker", help="ticker starts with")
    ap.add_argument("--lane")
    ap.add_argument("--out", help="write CSV here (default: print)")
    a = ap.parse_args()
    t0 = datetime.strptime(a.since, "%Y-%m-%d %H:%M").replace(tzinfo=ET).timestamp()
    t1 = datetime.strptime(a.until, "%Y-%m-%d %H:%M").replace(tzinfo=ET).timestamp()
    rows = read(a.table, t0, t1).to_pylist()
    rows = [r for r in rows if t0 <= r["ts"] < t1 and (not a.ticker or str(r.get("ticker", r.get("index_id", ""))).startswith(a.ticker)) and (not a.lane or r.get("lane") == a.lane)]
    import csv

    out = open(a.out, "w", newline="") if a.out else sys.stdout
    if rows:
        w = csv.DictWriter(out, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(f"{len(rows)} rows", file=sys.stderr)


if __name__ == "__main__":
    main()
