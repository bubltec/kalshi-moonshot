"""Ship old tape days to S3 as Parquet, so the local database stays small while the history stays reachable.

For every table in the tape (quotes, flow_1s, index_ticks, feed_ticks) and every complete UTC day, this writes one zstd Parquet file, uploads it with a
SHA-256 checksum that S3 itself verifies, reads the object's size and checksum back, and only then records it in `archive_manifest`. Local rows are
deleted last, and only for days older than ARCHIVE_HOT_DAYS, only when ARCHIVE_DELETE_LOCAL is on, and only if the manifest says that exact day is safely
in the bucket and the rows still in the database are the ones that were exported. Nothing is deleted on a failed upload, a mismatch or a doubt.

    s3://BUCKET/tape/<table>/day=YYYY-MM-DD/part-0.parquet      (the layout infra/archive_stack.py makes Athena tables for)
    s3://BUCKET/backups/<name>-YYYYMMDD.dump                    the nightly pg_dump files

It does nothing unless ARCHIVE_BUCKET is set. `python -m kalshi_moonshot archive` is a dry run; `--apply` uploads; `--apply --delete-local` also deletes.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from . import db
from .config import Settings

log = logging.getLogger("moonshot")

# table -> columns in the order they are written, with the Parquet type. infra/archive_stack.py defines the Athena tables from the same list
# (tests/test_archive.py checks the two agree).
TABLES: dict[str, list[tuple[str, str]]] = {
    "quotes": [("ts", "double"), ("ticker", "string"), ("bid", "double"), ("ask", "double"), ("last", "double"), ("sts", "bigint"), ("bid_size", "double"), ("ask_size", "double")],
    "flow_1s": [("ts", "bigint"), ("ticker", "string"), ("yes_vol", "double"), ("no_vol", "double"), ("n", "int"), ("vwap", "double")],
    "index_ticks": [("ts", "double"), ("index_id", "string"), ("value", "double"), ("avg60", "double"), ("quarter_avg", "double"), ("src_ms", "bigint")],
    "feed_ticks": [("ts", "double"), ("symbol", "string"), ("px", "double")],
}
GRACE = timedelta(hours=1)  # a day is archived once it ended this long ago, so late rows of the last minutes are in
CHUNK_SECONDS = 3600  # rows are read an hour at a time: bounded memory, short queries
EVERY = 6 * 3600.0  # seconds between nightly-thread passes (each is cheap when everything is already archived)


def enabled(s: Settings) -> bool:
    return bool(s.archive_bucket)


def day_bounds(day: str) -> tuple[float, float]:
    start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=UTC)
    return start.timestamp(), (start + timedelta(days=1)).timestamp()


def key_for(table: str, day: str) -> str:
    return f"tape/{table}/day={day}/part-0.parquet"


def _arrow_type(name: str) -> Any:
    import pyarrow as pa

    return {"double": pa.float64(), "bigint": pa.int64(), "int": pa.int32(), "string": pa.string()}[name]


def client_for(s: Settings) -> Any:
    """An S3 client. Credentials come from the environment (AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY in .env), created when needed so a changed .env applies."""
    import boto3

    return boto3.client("s3", region_name=s.archive_region)


# ---- finding days ----
def complete_days(tape_db: Any, table: str, now: datetime) -> list[str]:
    """UTC days that have rows in `table` and ended at least GRACE ago, oldest first."""
    first = tape_db.execute(f"SELECT MIN(ts) FROM {table}").fetchone()[0]
    if first is None:
        return []
    last_day = (now - GRACE).astimezone(UTC).date() - timedelta(days=1)  # the newest day that has fully ended
    day = datetime.fromtimestamp(float(first), UTC).date()
    out = []
    while day <= last_day:
        lo, hi = day_bounds(day.isoformat())
        if tape_db.execute(f"SELECT 1 FROM {table} WHERE ts >= ? AND ts < ? LIMIT 1", (lo, hi)).fetchone():
            out.append(day.isoformat())
        day += timedelta(days=1)
    return out


def count_rows(tape_db: Any, table: str, day: str) -> int:
    lo, hi = day_bounds(day)
    return int(tape_db.execute(f"SELECT COUNT(*) FROM {table} WHERE ts >= ? AND ts < ?", (lo, hi)).fetchone()[0])


def manifest(store: Any, table: str, day: str) -> Any:
    return store.db.execute("SELECT * FROM archive_manifest WHERE tbl = ? AND day = ?", (table, day)).fetchone()


# ---- export, upload, verify ----
def export_day(tape_db: Any, table: str, day: str, dest: Path) -> int:
    """Write one day of `table` to a Parquet file, an hour of rows at a time. Returns the number of rows written."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    cols = TABLES[table]
    schema = pa.schema([(name, _arrow_type(kind)) for name, kind in cols])
    lo, hi = day_bounds(day)
    rows = 0
    writer = pq.ParquetWriter(str(dest), schema, compression="zstd")
    try:
        t = lo
        while t < hi:
            chunk = tape_db.execute(
                f"SELECT {', '.join(name for name, _ in cols)} FROM {table} WHERE ts >= ? AND ts < ? ORDER BY ts", (t, min(t + CHUNK_SECONDS, hi))
            ).fetchall()
            if chunk:
                arrays = [pa.array([r[i] for r in chunk], type=schema.field(i).type) for i in range(len(cols))]
                writer.write_table(pa.Table.from_arrays(arrays, schema=schema))
                rows += len(chunk)
            t += CHUNK_SECONDS
    finally:
        writer.close()
    return rows


def sha256_b64(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return base64.b64encode(h.digest()).decode()


def upload_verified(client: Any, bucket: str, key: str, path: Path) -> str:
    """Put the file with its SHA-256 (S3 rejects the upload if the bytes do not match), then read the size and checksum back. Returns the checksum."""
    digest = sha256_b64(path)
    size = path.stat().st_size
    with path.open("rb") as f:
        client.put_object(Bucket=bucket, Key=key, Body=f, ChecksumSHA256=digest, ContentType="application/octet-stream")
    head = client.head_object(Bucket=bucket, Key=key, ChecksumMode="ENABLED")
    if int(head["ContentLength"]) != size or head.get("ChecksumSHA256") != digest:
        raise RuntimeError(f"s3://{bucket}/{key} does not match what was sent (size {head.get('ContentLength')} vs {size})")
    return digest


@dataclass
class Report:
    lines: list[str] = field(default_factory=list)
    uploaded: int = 0
    deleted_days: int = 0
    failed: int = 0

    def say(self, line: str) -> None:
        self.lines.append(line)
        log.info("archive: %s", line)


def archive_day(store: Any, tape_db: Any, client: Any, bucket: str, table: str, day: str, report: Report) -> bool:
    """Export and upload one table-day unless it is already in the manifest. Returns True when it is safely archived afterwards."""
    if manifest(store, table, day) is not None:
        return True
    with tempfile.TemporaryDirectory(prefix="archive-") as tmp:
        path = Path(tmp) / "part-0.parquet"
        rows = export_day(tape_db, table, day, path)
        if rows == 0:
            return False
        key = key_for(table, day)
        try:
            digest = upload_verified(client, bucket, key, path)
        except Exception as exc:
            report.failed += 1
            report.say(f"{table} {day}: upload failed ({exc}); nothing recorded, nothing deleted")
            return False
        size = path.stat().st_size
    store.db.execute(
        "INSERT OR REPLACE INTO archive_manifest (tbl, day, rows_n, bytes_n, sha256, s3_key, uploaded_ts, deleted_ts) VALUES (?, ?, ?, ?, ?, ?, ?, NULL)",
        (table, day, rows, size, digest, key, time.time()),
    )
    store.db.commit()
    report.uploaded += 1
    report.say(f"{table} {day}: {rows:,} rows -> {key} ({size / 1e6:.1f} MB)")
    return True


def delete_local_day(store: Any, tape_db: Any, table: str, day: str, report: Report) -> bool:
    """Delete one archived table-day from the local tape, but only if the manifest has it and the rows in the database are the ones exported."""
    m = manifest(store, table, day)
    if m is None or m["uploaded_ts"] is None:
        return False
    if m["deleted_ts"] is not None:
        return True
    local = count_rows(tape_db, table, day)
    if local > int(m["rows_n"]):
        report.say(f"{table} {day}: {local:,} rows locally but {m['rows_n']:,} archived (late rows?); kept")
        return False
    lo, hi = day_bounds(day)
    t = lo
    while t < hi:
        tape_db.execute(f"DELETE FROM {table} WHERE ts >= ? AND ts < ?", (t, min(t + CHUNK_SECONDS, hi)))
        tape_db.commit()
        t += CHUNK_SECONDS
    store.db.execute("UPDATE archive_manifest SET deleted_ts = ? WHERE tbl = ? AND day = ?", (time.time(), table, day))
    store.db.commit()
    report.deleted_days += 1
    report.say(f"{table} {day}: {local:,} local rows deleted (archived copy verified)")
    return True


def upload_backups(client: Any, bucket: str, folder: Path, report: Report) -> None:
    """Copy each pg_dump file in `folder` that is not in the bucket yet to backups/."""
    for path in sorted(folder.glob("*.dump")):
        key = f"backups/{path.name}"
        try:
            client.head_object(Bucket=bucket, Key=key)
            continue
        except Exception as exc:
            if "404" not in str(exc) and "Not Found" not in str(exc):
                report.failed += 1
                report.say(f"{path.name}: could not check the bucket ({exc})")
                continue
        try:
            upload_verified(client, bucket, key, path)
            report.uploaded += 1
            report.say(f"backup {path.name} -> {key} ({path.stat().st_size / 1e6:.1f} MB)")
        except Exception as exc:
            report.failed += 1
            report.say(f"backup {path.name}: upload failed ({exc})")


def run(
    s: Settings, store: Any, tape_db: Any, client: Any | None, *, apply: bool, delete_local: bool,
    tables: list[str] | None = None, hot_days: int | None = None, now: datetime | None = None,
) -> Report:
    """One pass. Without `apply` it only reports what it would do (and needs no credentials); with it, uploads; with `delete_local`, also deletes."""
    now = now or datetime.now(UTC)
    hot = hot_days if hot_days is not None else s.archive_hot_days
    cutoff = (now.astimezone(UTC).date() - timedelta(days=hot)).isoformat()  # days before this may be deleted locally
    report = Report()
    for table in tables or list(TABLES):
        days = complete_days(tape_db, table, now)
        todo = [d for d in days if manifest(store, table, d) is None]
        old = [d for d in days if d < cutoff and (m := manifest(store, table, d)) is not None and m["deleted_ts"] is None]
        if not apply:
            total = sum(count_rows(tape_db, table, d) for d in todo)
            report.say(f"{table}: {len(days)} complete days locally; {len(todo)} not archived yet ({total:,} rows); "
                       f"{len([d for d in todo if d < cutoff])} of those and {len(old)} already archived are older than {hot} days")
            if todo:
                with tempfile.TemporaryDirectory(prefix="archive-") as tmp:
                    path = Path(tmp) / "sample.parquet"
                    n = export_day(tape_db, table, todo[0], path)
                    raw = max(1, n)
                    report.say(f"  e.g. {todo[0]}: {n:,} rows -> {path.stat().st_size / 1e6:.1f} MB of Parquet ({path.stat().st_size / raw:.0f} bytes a row)")
            continue
        if client is None:
            raise RuntimeError("no S3 client")
        for day in todo:
            archive_day(store, tape_db, client, s.archive_bucket, table, day, report)
        if delete_local:
            for day in [d for d in days if d < cutoff]:
                delete_local_day(store, tape_db, table, day, report)
    if apply and client is not None and db.use_postgres():
        upload_backups(client, s.archive_bucket, s.data_dir / "backups", report)
    return report


def prune_local(s: Settings, store: Any, tape_db: Any) -> int:
    """What the tape's hourly clean-up does once the archive is on: delete only archived days older than ARCHIVE_HOT_DAYS, and only when deleting is switched on."""
    if not s.archive_delete_local:
        return 0
    now = datetime.now(UTC)
    cutoff = (now.date() - timedelta(days=s.archive_hot_days)).isoformat()
    report = Report()
    for table in TABLES:
        for day in complete_days(tape_db, table, now):
            if day < cutoff and manifest(store, table, day) is not None:
                delete_local_day(store, tape_db, table, day, report)
    return report.deleted_days


def status(store: Any) -> list[Any]:
    return store.db.execute("SELECT tbl, COUNT(*) AS days, SUM(rows_n) AS rows_n, SUM(bytes_n) AS bytes_n, SUM(CASE WHEN deleted_ts IS NULL THEN 0 ELSE 1 END) AS deleted_days, "
                            "MIN(day) AS first_day, MAX(day) AS last_day FROM archive_manifest GROUP BY tbl ORDER BY tbl").fetchall()


def fetch_day(s: Settings, client: Any, day: str, out: Path, tables: list[str] | None = None) -> list[Path]:
    """Download one day's Parquet files into `out` (for DuckDB, pandas or a one-off look). Does not touch the database."""
    out.mkdir(parents=True, exist_ok=True)
    got = []
    for table in tables or list(TABLES):
        dest = out / f"{table}-{day}.parquet"
        client.download_file(s.archive_bucket, key_for(table, day), str(dest))
        got.append(dest)
    return got


# ---- the background job ----
def start_nightly(get_settings: Any, make_store: Any, make_tape_db: Any) -> None:
    """A background thread: every few hours, upload whatever complete days and backups are not in the bucket yet, and delete archived days from the local tape
    if that is switched on. Nothing happens while ARCHIVE_BUCKET is empty."""

    def loop() -> None:
        time.sleep(600.0)  # let the bot settle after a start
        while True:
            s = get_settings()
            if enabled(s):
                try:
                    rep = run(s, make_store(), make_tape_db(), client_for(s), apply=True, delete_local=s.archive_delete_local)
                    if rep.failed:
                        log.warning("archive: %d uploads failed this pass; they are retried next time", rep.failed)
                except Exception:
                    log.exception("archive: pass failed")
            time.sleep(EVERY)

    threading.Thread(target=loop, name="archive", daemon=True).start()
