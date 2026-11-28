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
REWRITE = 10  # minutes of the bot's own tables shipped again on every pass (their rows are read by id, which spans about this many minutes)
FIRST_REWRITE = 70  # minutes of them on the first pass after a start (a restart or a new image leaves a gap)
MAX_BACKFILL = 360  # at most this many minutes are backfilled after a start, from the last file in S3
NO_FILES_BACKFILL = 120  # minutes backfilled when S3 has no file of the table at all
MAX_CATCHUP = 180  # at most this many minutes of the live buffer in one pass after a long gap; the rest follows on the next passes
SLICE = 30  # minutes per backfill slice (bounded memory)
BET_KEEP_S = 6 * 3600.0  # a bet is followed for its exit and settlement this long after its decision
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
STORE_TS = {"decisions": ("ts",), "screen_log": ("ts",), "orders": ("ts",)}  # a row is in the file of the minute it was written; a bet's later exit and settlement ship as `-b` update files (below)


def enabled(s: Settings) -> bool:
    return bool(s.archive_bucket) and bool(getattr(s, "live_ship", True))


def key_for(table: str, minute: int, suffix: str = "") -> str:
    day = datetime.fromtimestamp(minute * 60, UTC).strftime("%Y-%m-%d")
    return f"{PREFIX}/{table}/day={day}/m{minute}{suffix}.parquet"


_buf: dict[str, list[tuple]] = {n: [] for n in TAPE}
_buf_lock = threading.Lock()
BUF_KEEP_S = 1800.0  # seconds of rows kept in memory for the shipper (a few MB)
ID_WINDOW = {"decisions": 6000, "screen_log": 30000}  # the newest ids read each pass (primary key): about 390 decisions a minute, so REWRITE minutes
ID_WINDOW_FIRST = {"decisions": 30000, "screen_log": 30000}  # on the first pass after a start: about 75 minutes of decisions
_tee_first = 0.0  # when the buffer began to fill (0 = not yet): minutes before the next full one come from the database on a backfill


_tape_on = False  # set by start(role="tape"): only the tape recorder's process buffers and ships the tape tables (the bot writes none of them that matter)


def tee(rows: dict[str, list[tuple]]) -> None:
    """Called by Tape.flush after a commit with the rows just written. Appends to memory; trims what is older than BUF_KEEP_S. No database, no S3."""
    global _tee_first
    if not _tape_on:
        return
    now = time.time()
    with _buf_lock:
        if _tee_first == 0.0:
            _tee_first = now
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


def store_rows(store_db: Any, table: str, since_minute: int, first: bool = False) -> list[tuple]:
    """The recent rows of a store table, by primary key or the ts index only (never a scan), with `shipped_ts` appended."""
    cols = ", ".join(c for c, _ in STORE[table])
    if table == "orders":
        got = store_db.execute(f"SELECT {cols} FROM orders WHERE ts >= ? ORDER BY ts", (since_minute * 60.0 - 60.0,)).fetchall()  # idx_orders_ts
    else:
        top = store_db.execute(f"SELECT MAX(id) FROM {table}").fetchone()[0] or 0  # the primary key's max: an index lookup
        got = store_db.execute(f"SELECT {cols} FROM {table} WHERE id > ? ORDER BY id", (top - (ID_WINDOW_FIRST if first else ID_WINDOW)[table],)).fetchall()
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
        elif kind == "double":
            vals = [None if v is None else float(v) for v in vals]  # a database driver may hand back Decimal; the buffer's rows are floats already
        elif kind in ("bigint", "int"):
            vals = [None if v is None else int(v) for v in vals]
        arrays.append(pa.array(vals, type=schema.field(i).type))
    pq.write_table(pa.Table.from_arrays(arrays, schema=schema), str(dest), compression="zstd")


def put(client: Any, bucket: str, key: str, path: Path) -> None:
    data = path.read_bytes()
    client.put_object(Bucket=bucket, Key=key, Body=__import__("io").BytesIO(data), ChecksumSHA256=base64.b64encode(hashlib.sha256(data).digest()).decode(), ContentType="application/octet-stream")


def ship_rows(client: Any, bucket: str, table: str, minute: int, rows: list[tuple], suffix: str = "") -> int:
    """Write and upload one table-minute from rows already in hand. Returns the rows shipped (0: no file)."""
    if not rows:
        return 0
    with tempfile.TemporaryDirectory(prefix="liveship-") as tmp:
        path = Path(tmp) / "part.parquet"
        write_parquet(table, rows, path)
        put(client, bucket, key_for(table, minute, suffix), path)
    return len(rows)


_bets: dict[int, tuple] = {}  # decision id -> (decision ts, the state last shipped): the bets whose exit and settlement are still to follow
DECISION_COLS = [c for c, _ in STORE["decisions"]]
BET_STATE = ("outcome", "settled_ts", "exit_price", "exit_ts", "exit_reason", "contracts", "stake_usd")


def last_shipped_minute(client: Any, bucket: str, table: str, now: float) -> int | None:
    """The newest minute of `table` that has a file in S3 (today's and yesterday's day folders; one listing each), or None."""
    best = None
    for back in (0, 1):
        day = datetime.fromtimestamp(now - back * 86400, UTC).strftime("%Y-%m-%d")
        token = None
        while True:
            kw = {"Bucket": bucket, "Prefix": f"{PREFIX}/{table}/day={day}/"}
            if token:
                kw["ContinuationToken"] = token
            resp = client.list_objects_v2(**kw)
            for o in resp.get("Contents", []):
                name = o["Key"].rsplit("/", 1)[-1]
                digits = "".join(ch for ch in name[1:].split(".")[0].split("-")[0] if ch.isdigit())
                if name.startswith("m") and digits:
                    best = max(best or 0, int(digits))
            if not resp.get("IsTruncated"):
                break
            token = resp.get("NextContinuationToken")
        if best is not None:
            break
    return best


def backfill_tape(client: Any, bucket: str, tape_db: Any, tickers: list[str], index_ids: list[str], first: int, last: int, pause: float = 0.02) -> int:
    """Ship minutes first..last of the tape tables from the database after a restart or a new image. Every read is by the tape's own index (ticker, ts) or (index_id, ts): one
    small indexed query per ticker or index and slice, never a scan. Slices of SLICE minutes bound the memory; a pause between queries keeps the load off the host."""
    shipped = 0
    for lo_m in range(first, last + 1, SLICE):
        hi_m = min(lo_m + SLICE - 1, last)
        lo, hi = lo_m * 60.0, (hi_m + 1) * 60.0
        got: dict[str, dict[int, list[tuple]]] = {t: {} for t in TAPE}
        for table, keys, col in (("quotes", tickers, "ticker"), ("flow_1s", tickers, "ticker"), ("index_ticks", index_ids, "index_id"), ("index_fast", index_ids, "index_id")):
            cols = ", ".join(c for c, _ in TAPE[table])
            for key in keys:
                for row in tape_db.execute(f"SELECT {cols} FROM {table} WHERE {col} = ? AND ts >= ? AND ts < ? ORDER BY ts", (key, lo, hi)).fetchall():
                    got[table].setdefault(int(row[0] // 60), []).append(tuple(row))
                time.sleep(pause)
        for table, by_minute in got.items():
            for m, rows in sorted(by_minute.items()):
                shipped += ship_rows(client, bucket, table, m, sorted(rows, key=lambda r: r[0]))
    return shipped


def bet_rows(store_db: Any, now: float, first_pass: bool) -> list[tuple]:
    """The bets to look at this pass, by index only. A bet's decision row does NOT sit in the id window of the pass rows (its id comes from a reserved pool, `Store._reserved_id`),
    so bets are read through their own partial indexes: the open ones (`idx_decisions_bets_open`: action='bet' and no outcome yet), the ones tracked here that have since settled
    (by primary key), and on the first pass after a start the bets of the markets in the orders of the last BET_KEEP_S (`orders.ts` index, then `idx_decisions_ticker`)."""
    cols = ", ".join(DECISION_COLS)
    out: dict[int, tuple] = {}
    for r in store_db.execute(f"SELECT {cols} FROM decisions WHERE action = 'bet' AND outcome IS NULL AND ts > ?", (now - BET_KEEP_S,)).fetchall():
        out[r[0]] = tuple(r)
    gone = [i for i in _bets if i not in out]  # tracked, not open any more: settled (or sold and settled)
    for k in range(0, len(gone), 400):
        chunk = gone[k:k + 400]
        for r in store_db.execute(f"SELECT {cols} FROM decisions WHERE id IN ({','.join('?' * len(chunk))})", tuple(chunk)).fetchall():
            out[r[0]] = tuple(r)
    if first_pass:
        tickers = [r[0] for r in store_db.execute("SELECT DISTINCT ticker FROM orders WHERE ts > ?", (now - BET_KEEP_S,)).fetchall()]
        for k in range(0, len(tickers), 100):
            chunk = tickers[k:k + 100]
            for r in store_db.execute(f"SELECT {cols} FROM decisions WHERE ticker IN ({','.join('?' * len(chunk))}) AND action = 'bet'", tuple(chunk)).fetchall():
                out[r[0]] = tuple(r)
    return list(out.values())


def ship_bet_updates(client: Any, bucket: str, store_db: Any, now: float, first_pass: bool = False) -> int:
    """A bet is shipped when it is new or its state changed (exit, settlement): as a unique `m<minute>-b<epoch>.parquet` file in the minute of its latest time, so no file overwrites
    another. A reader keeps the newest copy of each id. The bets followed are forgotten BET_KEEP_S after their decision."""
    for i in [i for i, (ts, _) in _bets.items() if ts < now - BET_KEEP_S]:
        del _bets[i]
    by_minute: dict[int, list[tuple]] = {}
    for row in bet_rows(store_db, now, first_pass):
        rec = dict(zip(DECISION_COLS, row, strict=True))
        state = tuple(rec[c] for c in BET_STATE)
        if rec["id"] in _bets and _bets[rec["id"]][1] == state:
            continue
        _bets[rec["id"]] = (rec["ts"], state)
        when = max(t for t in (rec["ts"], rec["exit_ts"], rec["settled_ts"]) if t is not None)
        by_minute.setdefault(int(when // 60), []).append((*row, now))
    n = 0
    for m, rows in sorted(by_minute.items()):
        n += ship_rows(client, bucket, "decisions", m, rows, suffix=f"-b{int(now)}")
    return n


def pass_once(client: Any, bucket: str, store_db: Any, done: int | None, now: float, role: str = "both", *, tape_db: Any = None, tickers: Any = None) -> tuple[int, int]:
    """One pass. `done` is the newest tape minute fully shipped (None on the first pass after a start, which backfills). Returns (the new `done`, rows shipped).
    Tape (recorder): minutes still in the live buffer come from memory; on the first pass the minutes between the newest file in S3 and the buffer's start come from the tape database by index.
    Store (bot): the newest decisions, screen_log and orders rows, then the bets followed for their exit and settlement."""
    closed = int((now - GRACE) // 60) - 1  # the newest minute that ended at least GRACE ago
    first_pass = done is None
    shipped = 0
    if role in ("tape", "both"):
        buf_from = int(_tee_first // 60) + 1 if _tee_first else closed + 1  # the first minute the buffer holds whole
        if first_pass:
            last = last_shipped_minute(client, bucket, "quotes", now)
            first = max(closed - MAX_BACKFILL, (last + 1) if last is not None else closed - NO_FILES_BACKFILL)
            if tape_db is not None and first < min(buf_from, closed + 1):
                with _buf_lock:
                    index_ids = sorted({r[1] for t in ("index_ticks", "index_fast") for r in _buf[t]})
                sets = set(tickers() if tickers else ())
                if store_db is not None:
                    sets |= {r[0] for r in store_db.execute("SELECT ticker FROM windows WHERE close_ts >= ? AND close_ts <= ?", (first * 60 - 60, (closed + 16) * 60)).fetchall()}
                try:
                    shipped += backfill_tape(client, bucket, tape_db, sorted(sets), index_ids, first, min(buf_from, closed + 1) - 1)
                except Exception:  # 2026-11-28: a failed backfill must never stop the live minutes (after the 13:06 deploy no quote reached S3: the backfill raised on every pass and took the live shipping with it)
                    log.exception("liveship: the backfill of minutes %s to %s failed; the live minutes are shipped all the same", first, min(buf_from, closed + 1) - 1)
            first = max(first, buf_from)
        else:
            first = done + 1
        first = max(first, closed - MAX_CATCHUP)
        for minute in range(first, closed + 1):
            for table in TAPE:
                shipped += ship_rows(client, bucket, table, minute, buffered(table, minute))
    if role in ("store", "both"):
        lo = closed - (FIRST_REWRITE if first_pass else REWRITE) + 1
        for table in STORE:
            names = [c for c, _ in STORE[table]]
            by_minute: dict[int, list[tuple]] = {}
            for row in store_rows(store_db, table, lo, first_pass):
                for m in minute_of(table, row, names):
                    if lo <= m <= closed:
                        by_minute.setdefault(m, []).append(row)
            for m, rows in sorted(by_minute.items()):
                shipped += ship_rows(client, bucket, table, m, rows)
        shipped += ship_bet_updates(client, bucket, store_db, now, first_pass)
    return closed, shipped


def start(get_settings: Any, make_store: Any, role: str = "store", make_tape_db: Any = None, tickers: Any = None) -> None:
    """The background thread. Idle until ARCHIVE_BUCKET is set; a failed pass is retried on the next one (the keys are fixed, so nothing is duplicated). The first pass after a
    start BACKFILLS what a restart or a new image left out: the tape minutes since the newest file in S3 (by index, from the database) and the last 70 minutes of the bot's tables."""

    global _tape_on
    _tape_on = role in ("tape", "both")

    def loop() -> None:
        log.info("liveship (%s): thread started; the first pass in 120 s backfills what the last start left out", role)
        time.sleep(120.0)  # let the bot settle and the tape fill after a start
        done: int | None = None
        store = tape_db = None
        while True:
            s = get_settings()
            if enabled(s):
                try:
                    if store is None:
                        store = make_store()
                    if tape_db is None and make_tape_db is not None and role != "store":
                        tape_db = make_tape_db()
                    done, n = pass_once(archive.client_for(s), s.archive_bucket, store.db if store else None, done, time.time(), role, tape_db=tape_db, tickers=tickers)
                    if done % 10 == 0 or n == 0 and done % 3 == 0:
                        log.info("liveship (%s): through minute %s (%d rows this pass)", role, done, n)
                except Exception:
                    log.exception("liveship: pass failed; retried next time")
                    store = tape_db = None  # a dropped connection is made again; `done` stays, so the minutes are shipped on the next pass
            time.sleep(EVERY)

    threading.Thread(target=loop, name="liveship", daemon=True).start()
