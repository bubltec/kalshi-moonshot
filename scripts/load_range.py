"""Load archived tape days from S3 into THIS machine's tape database (the Mac's Docker Postgres), so replay scripts and studies see cloud history.
Downloads each day's Parquet (or reuses a folder from stage_range.py) and inserts it. A table-day that already has rows is skipped, so re-running a range is safe.
Reads S3 and writes the tape of whatever DATABASE_URL points at, so run it where that is the database you mean (the Mac), never against the cloud's.

    docker compose exec -T bot python - 2026-10-28 2026-10-31 < scripts/load_range.py        # all tape tables
    python scripts/load_range.py 2026-10-28 2026-10-31 --table quotes --table index_ticks
    python scripts/load_range.py 2026-10-28 2026-10-31 --from ~/stage                         # files already staged
"""
import argparse
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

from kalshi_moonshot import archive, config, db

ap = argparse.ArgumentParser()
ap.add_argument("start")
ap.add_argument("end")
ap.add_argument("--table", action="append", choices=list(archive.TABLES))
ap.add_argument("--from", dest="src", help="a folder of <table>-<day>.parquet files instead of downloading")
args = ap.parse_args(sys.argv[1:])

s = config.reload()
tape = db.connect(s.tape_path, "tape", shared=True)
first, last = date.fromisoformat(args.start), date.fromisoformat(args.end)
tables = args.table or list(archive.TABLES)
folder = Path(args.src).expanduser() if args.src else Path(tempfile.mkdtemp(prefix="load_range_"))
client = None if args.src else archive.client_for(s)
print(f"loading {first}..{last} into {'postgres' if db.use_postgres() else s.tape_path}")
for n in range((last - first).days + 1):
    day = (first + timedelta(days=n)).isoformat()
    for table in tables:
        path = folder / f"{table}-{day}.parquet"
        try:
            if not path.exists():
                archive.fetch_day(s, client, day, folder, [table])
            rows = archive.load_day(tape, table, day, path)
        except Exception as exc:
            tape.rollback()
            print(f"{table} {day}: {str(exc)[:100]}")
            continue
        print(f"{table} {day}: {rows:,} rows loaded" if rows else f"{table} {day}: already there, skipped")
        if not args.src:
            path.unlink(missing_ok=True)
