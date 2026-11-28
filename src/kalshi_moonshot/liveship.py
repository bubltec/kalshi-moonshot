"""Ship the live tape and the bot's own records to S3 a minute at a time, so a review of the last few hours reads S3 and never the live host.

2026-11-28 (user: "Why are you pulling from the host? We need to pull from a different log source ... pipe logs to s3 ... You're just affecting the host from operating properly"). The nightly archive (archive.py) only sends complete UTC days; today was reachable only through `cloud.sh sql` on the 2-CPU host that trades. This is the missing piece: a background thread (the one kind of timer the architecture allows: a job that builds or refreshes data) that, every minute, writes each CLOSED minute of the tape tables and of decisions, screen_log and orders to one small Parquet file:

    s3://BUCKET/tape/live/<table>/day=YYYY-MM-DD/m<minute epoch>.parquet

The key is fixed by the table and the minute, so shipping a minute twice overwrites the same object: a restart, a retry or a re-ship never duplicates a row. It stays under the `tape/` prefix the archive writer's IAM policy already allows. Tape tables (append-only) ship each minute once. decisions, screen_log and orders change after they are written (a fill, a sale, a settlement, a decision row written behind by the queue), so the last REWRITE minutes are shipped again each pass, and a decision is also shipped in the minute of its exit and of its settlement; every row carries `shipped_ts`, and a reader takes the newest copy of an `id`. Reads and writes here are a background job's: nothing on a tick, a buy or a sell path calls this module.

2026-11-28 (first version, withdrawn the same morning): it read each minute from the tape tables with `WHERE ts >= ? AND ts < ?`; the tape's indexes are (ticker, ts) and (index_id, ts), so every minute was a sequential scan of two weeks of quotes, one ran for 13 minutes and the host slowed to a crawl. NOW THE SHIPPER READS NO TAPE TABLE: `Tape.flush` hands the rows it has just written to `tee()` (memory), and a pass writes the minutes from that buffer. decisions, screen_log and orders are read with the primary key or the `ts` index only (`id >= max id - N`, `ts >= ?`), never a scan.

It does nothing unless ARCHIVE_BUCKET is set and LIVE_SHIP is not off. Read side: `scripts/live_pull.py`, or Athena over the `live_*` tables (`infra/archive_stack.py`).
"""

from __future__ import annotations

import base64
import hashlib
import logging
import tempfile
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import archive
from .config import Settings

log = logging.getLogger("moonshot")

EVERY = 60.0  # seconds between passes
GRACE = 45.0  # a minute is shipped once it ended this long ago: rows written behind by a queue are in
REWRITE = 20  # minutes of the mutable tables shipped again on every pass
BACKFILL = 30  # minutes shipped when the thread starts (the same keys, so no duplicates)
MAX_CATCHUP = 180  # at most this many minutes in one pass after a long gap; the rest follows on the next passes
PREFIX = "tape/live"

# source "tape": append-only rows from the tape database; "store": rows of the bot's own database that change after they are written.
TAPE = {name: archive.TABLES[name] for name in ("quotes", "flow_1s", "index_ticks", "index_fast")}
STORE: dict[str, list[tuple[str, str]]] = {
    "decisions": [("id", "bigint"), ("ts", "double"), ("ticker", "string"), ("lane", "string"), ("mode", "string"), ("action", "string"), ("side", "string"), ("bid", "double"), ("ask", "double"),
                  ("model_p", "double"), ("edge", "double"), ("reason", "string"), ("close_time", "string"), ("outcome", "string"), ("settled_ts", "double"), ("exit_price", "double"),
                  ("exit_ts", "double"), ("exit_reason", "string"), ("contracts", "double"), ("stake_usd", "double"), ("event_ticker", "string"), ("version", "string"),
                  ("factors", "string"), ("exit_factors", "string")],
    "screen_log": [("id", "bigint"), ("ts", "double"), ("lane", "string"), ("ticker", "string"), ("side", "string"), ("ask", "double"), ("model_p", "double"), ("edge", "double"), ("reason", "string")],
    "orders": [("id", "bigint"), ("ts", "double"), ("ticker", "string"), ("env", "string"), ("count", "double"), ("price", "double"), ("fill_count", "double"), ("cost_usd", "double"),
               ("fee_usd", "double"), ("order_id", "string"), ("side", "string"), ("contract", "string")],
}
LIVE_TABLES: dict[str, list[tuple[str, str]]] = {**TAPE, **{n: [*c, ("shipped_ts", "double")] for n, c in STORE.items()}}
# which timestamps put a store row in a minute: its own, and for decisions the exit's and the settlement's
STORE_TS = {"decisions": ("ts", "exit_ts", "settled_ts"), "screen_log": ("ts",), "orders": ("ts",)}


def enabled(s: Settings) -> bool:
    return bool(s.archive_bucket) and bool(getattr(s, "live_ship", True))


def key_for(table: str, minute: int) -> str:
    day = datetime.fromtimestamp(minute * 60, UTC).strftime("%Y-%m-%d")
    return f"{PREFIX}/{table}/day={day}/m{minute}.parquet"


_buf: dict[str, list[tuple]] = {n: [] for n in TAPE}
_buf_lock = threading.Lock()
BUF_KEEP_S = 1800.0  # seconds of rows kept in memory for the shipper (a few MB)
ID_WINDOW = {"decisions": 3000, "screen_log": 30000}  # the newest ids read each pass (primary key), enough to cover REWRITE minutes and the settlements of recent bets


def tee(rows: dict[str, list[tuple]]) -> None:
    """Called by Tape.flush after a commit with the rows just written. Appends to memory; trims what is older than BUF_KEEP_S. No database, no S3."""
    now = time.time()
    with _buf_lock:
        for table, got in rows.items():
            if table in _buf and got:
                buf = _buf[table]
                buf.extend(tuple(r) for r in got)
                if len(buf) > 200_000 or (buf and buf[0][0] < now - BUF_KEEP_S - 600):
                    cut = now - BUF_KEEP_S
                    _buf[table] = [r for r in buf if r[0] >= cut]


def buffered(table: str, minute: int) -> list[tuple]:
    lo, hi = minute * 60.0, minute * 60.0 + 60.0
    with _buf_lock:
        return sorted((r for r in _buf[table] if lo <= r[0] < hi), key=lambda r: r[0])


def store_rows(store_db: Any, table: str, since_minute: int) -> list[tuple]:
    """The recent rows of a store table, by primary key or the ts index only (never a scan), with `shipped_ts` appended."""
    cols = ", ".join(c for c, _ in STORE[table])
    if table == "orders":
        got = store_db.execute(f"SELECT {cols} FROM orders WHERE ts >= ? ORDER BY ts", (since_minute * 60.0 - 3600.0,)).fetchall()  # idx_orders_ts
    else:
        top = store_db.execute(f"SELECT MAX(id) FROM {table}").fetchone()[0] or 0  # the primary key's max: an index lookup
        got = store_db.execute(f"SELECT {cols} FROM {table} WHERE id > ? ORDER BY id", (top - ID_WINDOW[table],)).fetchall()
    now = time.time()
    return [(*tuple(r), now) for r in got]


def minute_of(table: str, row: tuple, names: list[str]) -> list[int]:
    return sorted({int(row[names.index(c)] // 60) for c in STORE_TS[table] if row[names.index(c)] is not None})


def write_parquet(table: str, rows: list[tuple], dest: Path) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    cols = LIVE_TABLES[table]
    schema = pa.schema([(name, archive._arrow_type(kind)) for name, kind in cols])
    arrays = []
    for i, (_, kind) in enumerate(cols):
        vals = [r[i] for r in rows]
        if kind == "string":
            vals = [None if v is None else str(v) for v in vals]
        arrays.append(pa.array(vals, type=schema.field(i).type))
    pq.write_table(pa.Table.from_arrays(arrays, schema=schema), str(dest), compression="zstd")


def put(client: Any, bucket: str, key: str, path: Path) -> None:
    data = path.read_bytes()
    client.put_object(Bucket=bucket, Key=key, Body=__import__("io").BytesIO(data), ChecksumSHA256=base64.b64encode(hashlib.sha256(data).digest()).decode(), ContentType="application/octet-stream")


def ship_rows(client: Any, bucket: str, table: str, minute: int, rows: list[tuple]) -> int:
    """Write and upload one table-minute from rows already in hand. Returns the rows shipped (0: no file)."""
    if not rows:
        return 0
    with tempfile.TemporaryDirectory(prefix="liveship-") as tmp:
        path = Path(tmp) / "part.parquet"
        write_parquet(table, rows, path)
        put(client, bucket, key_for(table, minute), path)
    return len(rows)


def pass_once(client: Any, bucket: str, store_db: Any, done: int | None, now: float) -> tuple[int, int]:
    """One pass. `done` is the newest tape minute fully shipped (None on the first pass: only what the memory holds is shipped). Returns (the new `done`, rows shipped)."""
    closed = int((now - GRACE) // 60) - 1  # the newest minute that ended at least GRACE ago
    first = closed - BACKFILL if done is None else done + 1
    first = max(first, closed - MAX_CATCHUP)
    shipped = 0
    for minute in range(first, closed + 1):
        for table in TAPE:
            shipped += ship_rows(client, bucket, table, minute, buffered(table, minute))
    lo = closed - REWRITE + 1
    for table in STORE:  # the mutable tables: the newest rows, grouped by the minutes they belong to, the last REWRITE minutes again
        names = [c for c, _ in STORE[table]]
        by_minute: dict[int, list[tuple]] = {}
        for row in store_rows(store_db, table, lo):
            for m in minute_of(table, row, names):
                if lo <= m <= closed:
                    by_minute.setdefault(m, []).append(row)
        for m, rows in sorted(by_minute.items()):
            shipped += ship_rows(client, bucket, table, m, rows)
    return closed, shipped


def start(get_settings: Any, make_store: Any) -> None:
    """The background thread. Idle until ARCHIVE_BUCKET is set; a failed pass is retried on the next one (the keys are fixed, so nothing is duplicated)."""

    def loop() -> None:
        time.sleep(120.0)  # let the bot settle and the tape fill after a start
        done: int | None = None
        store = None
        while True:
            s = get_settings()
            if enabled(s):
                try:
                    if store is None:
                        store = make_store()
                    done, n = pass_once(archive.client_for(s), s.archive_bucket, store.db, done, time.time())
                    if done % 10 == 0:
                        log.info("liveship: through minute %s (%d rows this pass)", done, n)
                except Exception:
                    log.exception("liveship: pass failed; retried next time")
                    store = None  # a dropped connection is made again
            time.sleep(EVERY)

    threading.Thread(target=loop, name="liveship", daemon=True).start()
