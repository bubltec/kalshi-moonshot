"""The bot's database (Postgres; see db.py). Every decision is logged so the model can be scored later.

Settled decisions are also indexed in a local full-text table. The next estimate can
retrieve similar past cases. Prices are not stored in that index.
"""

from __future__ import annotations

import json
import logging
import queue
import shutil
import sqlite3
import threading
import time
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from . import db
from .calibration import Calibrator
from .rag import Rag

_log = logging.getLogger(__name__)

# Bump this whenever filters, prompt, or model logic change enough that old decisions
# should not be scored together with new ones. Every new decision is tagged with it.
#   v1  everything logged before versions existed (included live-number markets and sports totals)
#   v2  skips live-number markets (SKIP_NUMERIC); still allowed both sides of one game and falling asks
#   v3  at most one side of a mutually exclusive event; moonshot skips a falling ask;
#       stop-loss sells (or paper-sells) when under 1h left and down 30%
#   v4  news is last 3 days, newest first with age; prompt treats only fresh facts as leverage
#   v5  steady takes a rising favorite unless the model is more than STEADY_MAX_LAG under the ask
#   v6  lanes are data (lanes.py); the estimate is weighed against our settled record before the
#       bet rule (calibration.py); the similar-case index is structured with base rates (rag.py);
#       commodity ladder lane added
STRATEGY_VERSION = "v6"
LEGACY_VERSION = "v1"

SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  ticker TEXT NOT NULL,
  title TEXT,
  bid REAL, ask REAL,
  model_p REAL, edge REAL,
  action TEXT NOT NULL,          -- 'bet' or 'pass'
  reason TEXT,
  mode TEXT NOT NULL,
  close_time TEXT,
  outcome TEXT,                  -- 'yes' / 'no' once settled
  settled_ts REAL,
  lane TEXT DEFAULT 'moonshot',  -- 'moonshot' or 'steady'
  context TEXT,                  -- news headlines the model was shown
  category TEXT,                 -- Kalshi event category
  version TEXT,                  -- strategy version that produced this decision
  event_ticker TEXT,             -- Kalshi event; used to block a second side of a mutex event
  exit_price REAL,               -- bid we sold (or would have sold) at; null = held to settlement
  exit_ts REAL,
  exit_reason TEXT,
  cal_p REAL,                    -- probability after blending the estimate with our record
  side TEXT DEFAULT 'yes',       -- contract bought; for 'no', ask/bid/model_p are NO-side numbers
  contracts REAL,                -- paper or real size sized by sizing.py (first bet per market only)
  stake_usd REAL                 -- cost incl. fee of those contracts
);
CREATE INDEX IF NOT EXISTS idx_decisions_ticker ON decisions(ticker);

CREATE TABLE IF NOT EXISTS orders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  ticker TEXT NOT NULL,
  env TEXT NOT NULL,
  count REAL NOT NULL,
  price REAL NOT NULL,
  fill_count REAL NOT NULL,
  cost_usd REAL NOT NULL,        -- what actually filled, incl. fee
  fee_usd REAL NOT NULL,
  order_id TEXT,
  side TEXT DEFAULT 'buy',       -- 'buy' or 'sell'
  contract TEXT DEFAULT 'yes'    -- which contract: 'yes' or 'no'
);
CREATE INDEX IF NOT EXISTS idx_orders_ts ON orders(ts);

CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,            -- scan | evaluate | resolve | stop_loss
  payload TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'pending',  -- pending | running | done | dead | expired
  run_at REAL NOT NULL,          -- not before this time
  created_ts REAL NOT NULL,
  started_ts REAL,
  finished_ts REAL,
  attempts INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 3,
  expires_ts REAL,               -- a pending job past this is dropped, not run late
  stage TEXT,                    -- 'placing' marks the moment an order may have gone out
  dedupe_key TEXT,
  last_error TEXT,
  result TEXT,
  priority INTEGER NOT NULL DEFAULT 0  -- higher runs first; fast lanes jump the queue
);

CREATE TABLE IF NOT EXISTS lane_versions (
  lane TEXT NOT NULL, ver TEXT NOT NULL, first_ts REAL NOT NULL, settings TEXT NOT NULL,
  PRIMARY KEY (lane, ver)
);

CREATE TABLE IF NOT EXISTS archive_manifest (
  tbl TEXT NOT NULL, day TEXT NOT NULL, rows_n INTEGER NOT NULL, bytes_n INTEGER NOT NULL, sha256 TEXT NOT NULL, s3_key TEXT NOT NULL,
  uploaded_ts REAL NOT NULL, deleted_ts REAL,
  PRIMARY KEY (tbl, day)
);

CREATE TABLE IF NOT EXISTS shard_moves (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  src_shard INTEGER NOT NULL,
  dst_shard INTEGER NOT NULL,
  usd REAL NOT NULL,
  reason TEXT,
  transfer_id TEXT
);

CREATE TABLE IF NOT EXISTS screen_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  lane TEXT NOT NULL,
  ticker TEXT NOT NULL,
  side TEXT NOT NULL,
  ask REAL,
  model_p REAL,                  -- our chance that this contract pays (null if the ladder could not be priced)
  edge REAL,
  reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_screen_log ON screen_log(ticker, ts);

CREATE TABLE IF NOT EXISTS shadow (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  ticker TEXT NOT NULL,
  series TEXT NOT NULL,
  minutes_left REAL NOT NULL,
  yes_bid REAL, yes_ask REAL,
  model_p REAL,                  -- our chance of YES at that moment (null if unpriceable)
  strike REAL,
  note TEXT,
  outcome TEXT,                  -- 'yes' / 'no' once settled
  settled_ts REAL
);
CREATE INDEX IF NOT EXISTS idx_shadow_ticker ON shadow(ticker, ts);

CREATE TABLE IF NOT EXISTS position_checks (
  ticker TEXT NOT NULL, lane TEXT NOT NULL, mark INTEGER NOT NULL,  -- one look at an open position per mark: seconds left it stands for (240, 210 ... 30, 10); see checkins.py
  side TEXT, ts REAL NOT NULL, left_s REAL, close_ts REAL,
  entry REAL,                    -- the price paid, in the side's terms
  bid REAL, other_ask REAL,      -- what the side could be sold for, and what the other side costs
  strike REAL, spot REAL, spot_age REAL,
  through REAL,                  -- signed share of the strike the index is through against the side (positive = on the losing side)
  through_on INTEGER, through_kept INTEGER,  -- the strike-through watch's own state
  features TEXT,                 -- JSON: the full feature snapshot (factors.snapshot_now) at that moment
  PRIMARY KEY (ticker, lane, mark)
);
CREATE INDEX IF NOT EXISTS idx_position_checks_ts ON position_checks(ts);
CREATE TABLE IF NOT EXISTS shock_log (  -- every new low of the spot's move against a held bet (3 bp or worse), whether or not the shock exit sold: the data per-asset shock settings get fitted on. The outcome is joined from decisions by ticker and lane
  ts REAL NOT NULL, ticker TEXT NOT NULL, lane TEXT NOT NULL, series TEXT, side TEXT,
  left_s REAL, entry REAL, bid REAL,
  cushion_bp REAL,               -- bp the spot sits on the winning side of its reference (negative = through)
  move_bp REAL, worst_bp REAL,   -- the latest shock_seconds move, and the worst one inside the shock memory (negative = against the bet)
  fired INTEGER                  -- 0 not sold, 1 sold, 2 would have sold (a log-only watch)
);
CREATE INDEX IF NOT EXISTS idx_shock_log_ticker ON shock_log(ticker);

-- 2026-11-12: nothing writes this table any more (the behavior-cases helper was removed: it grew to 1.6 GB and froze the host, and no lane read its output). The rows stay for scripts/behavior_walkforward.py and scripts/per_coin_forecast_study.py.
CREATE TABLE IF NOT EXISTS behavior_cases (
  ticker TEXT NOT NULL, side TEXT NOT NULL, ts REAL NOT NULL, kind TEXT NOT NULL,  -- kind: 'moment' (a sampled moment of a finished window) or 'buy' (our own real buy)
  series TEXT, close_ts REAL,
  features TEXT NOT NULL,        -- JSON: the full feature snapshot (factors.snapshot_full) at that moment
  won INTEGER, reach99 INTEGER,  -- did the side settle at $1; did its bid touch 99c before the close
  low_bid REAL, minutes_to_99 REAL,
  PRIMARY KEY (ticker, side, ts, kind)
);
CREATE INDEX IF NOT EXISTS idx_behavior_series ON behavior_cases(series, close_ts);

CREATE TABLE IF NOT EXISTS scalp_cases (
  ticker TEXT NOT NULL, side TEXT NOT NULL, ts REAL NOT NULL,  -- one attempt: a resting bid at the bid of a 30-48c side, replayed on the tape (scalp.py)
  series TEXT, close_ts REAL,
  features TEXT NOT NULL,        -- JSON: the full feature snapshot at that moment (factors.snapshot_full)
  filled INTEGER,                -- the bid filled (the side's ask came down to it) within the wait
  sell_hit INTEGER,              -- the resting sell above it filled
  ret_taker REAL, ret_hold REAL, -- return per dollar if the unsold remainder is sold at the bid / held to the result; NULL when the bid never filled
  PRIMARY KEY (ticker, side, ts)
);
CREATE INDEX IF NOT EXISTS idx_scalp_close ON scalp_cases(close_ts);

-- 2026-11-12: nothing writes or reads this table any more (the vector builder was removed with the behavior-cases helper). The rows stay.
CREATE TABLE IF NOT EXISTS curve_vectors (
  ticker TEXT NOT NULL, minute INTEGER NOT NULL,  -- one vector point per finished crypto window and decision minute (vectors.py)
  series TEXT, close_ts REAL,
  vec TEXT NOT NULL,             -- JSON: the coin's path since the open, the 10-second tail, the hour and 3-hour context, Kalshi's mid per minute and its 10-second tail, each as distances (see vectors.py)
  bid REAL, ask REAL,            -- the YES quote at the decision
  y INTEGER,                     -- the window finished above its target
  hit_yes INTEGER, hit_no INTEGER,  -- a resting sell 3c above the price paid would have filled within 3 minutes, buying YES / buying NO at the quote
  PRIMARY KEY (ticker, minute)
);
CREATE INDEX IF NOT EXISTS idx_curve_vectors_series ON curve_vectors(series, close_ts);

CREATE TABLE IF NOT EXISTS manual_entries (
  trade_id TEXT PRIMARY KEY,     -- Kalshi's id for the fill
  ts REAL NOT NULL,
  ticker TEXT NOT NULL,
  side TEXT NOT NULL,            -- contract bought: 'yes' or 'no'
  price REAL,                    -- price paid, in that side's own terms
  count REAL,
  order_id TEXT,
  close_ts REAL,
  factors TEXT,                  -- JSON entry context at the moment of the fill (factors.py); empty when the tape had none
  template INTEGER NOT NULL DEFAULT 0,  -- 1 = you pointed this buy out as a good example; only these teach the factors
  note TEXT
);
CREATE INDEX IF NOT EXISTS idx_manual_entries ON manual_entries(ticker, ts);

CREATE TABLE IF NOT EXISTS fills (
  fill_id TEXT PRIMARY KEY,
  ts REAL NOT NULL,
  ticker TEXT NOT NULL,
  book_side TEXT NOT NULL,       -- 'bid' = bought YES at yes_price; 'ask' = sold YES (= bought NO at no_price)
  count REAL NOT NULL,
  yes_price REAL NOT NULL,
  no_price REAL,
  fee REAL,
  taker INTEGER,
  order_id TEXT,
  action TEXT, side TEXT         -- Kalshi's own labels, kept for reference
);
CREATE INDEX IF NOT EXISTS idx_fills_ticker ON fills(ticker, ts);

CREATE TABLE IF NOT EXISTS settlements (
  ticker TEXT NOT NULL,
  settled_ts REAL NOT NULL,
  result TEXT,                   -- 'yes' / 'no'
  yes_count REAL, no_count REAL, -- what you held when it settled
  yes_cost REAL, no_cost REAL,
  fee REAL,
  PRIMARY KEY (ticker, settled_ts)
);

CREATE TABLE IF NOT EXISTS market_info (
  ticker TEXT PRIMARY KEY,
  series TEXT, open_ts REAL, close_ts REAL, result TEXT, strike REAL
);

CREATE TABLE IF NOT EXISTS windows (
  ticker TEXT PRIMARY KEY,
  series TEXT NOT NULL,
  close_ts INTEGER NOT NULL,
  strike REAL,
  result TEXT,                   -- 'yes' / 'no'
  fetched_ts REAL NOT NULL,
  settled_value REAL             -- the value the window settled on (Kalshi's expiration_value: the price of the underlying, not the contract's); NULL until read
);

CREATE TABLE IF NOT EXISTS window_marks (
  ticker TEXT PRIMARY KEY,
  series TEXT NOT NULL,
  close_ts INTEGER NOT NULL,
  strike REAL,
  result TEXT,                   -- 'yes' / 'no', from the stream's result event
  noted_ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_window_marks_series ON window_marks(series, close_ts);

CREATE TABLE IF NOT EXISTS grid_obs (
  ticker TEXT NOT NULL, series TEXT NOT NULL, side TEXT NOT NULL,
  mband INTEGER NOT NULL, pband INTEGER NOT NULL, rule TEXT NOT NULL DEFAULT 'trail',
  roi REAL NOT NULL, close_ts INTEGER NOT NULL, alpha TEXT, beta TEXT, edge TEXT,
  PRIMARY KEY (ticker, side, mband, pband, rule)
);
CREATE INDEX IF NOT EXISTS idx_grid_obs ON grid_obs(series, rule, mband, pband);
CREATE TABLE IF NOT EXISTS grid_done (ticker TEXT NOT NULL, rule TEXT NOT NULL DEFAULT 'trail', PRIMARY KEY (ticker, rule));
CREATE TABLE IF NOT EXISTS grid_meta (k TEXT PRIMARY KEY, v TEXT);

CREATE TABLE IF NOT EXISTS candles (
  ticker TEXT NOT NULL,
  end_ts INTEGER NOT NULL,       -- the minute this candle closes
  bid_c REAL, ask_c REAL,        -- YES bid / ask at the close of the minute
  bid_lo REAL, ask_hi REAL,
  last_c REAL,                   -- last traded YES price in the minute
  volume REAL, oi REAL,
  bid_hi REAL, ask_lo REAL,      -- the minute's best bid and cheapest ask: what a resting order could catch
  PRIMARY KEY (ticker, end_ts)
);

CREATE TABLE IF NOT EXISTS watchlist (
  lane TEXT NOT NULL,
  event_ticker TEXT NOT NULL,
  payload TEXT NOT NULL DEFAULT '{}',  -- close time, category, series from the scoping pass
  scoped_ts REAL NOT NULL,
  PRIMARY KEY (lane, event_ticker)
);
CREATE TABLE IF NOT EXISTS hour_slices (
  hour_end REAL NOT NULL,
  hour_et INTEGER NOT NULL,
  dow INTEGER NOT NULL,
  family TEXT NOT NULL,
  n INTEGER NOT NULL,
  won INTEGER NOT NULL,
  pnl REAL NOT NULL,
  avg_ask REAL,
  PRIMARY KEY (hour_end, family)
);
CREATE TABLE IF NOT EXISTS risk_readings (
  ticker TEXT NOT NULL,
  ts REAL NOT NULL,              -- the 30 s mark the reading was taken at
  x TEXT NOT NULL,               -- the fifteen YES readings, riskscore.NAMES' order (JSON list)
  yes_bid REAL,
  yes_ask REAL,
  age REAL,                      -- seconds since the market opened
  PRIMARY KEY (ticker, ts)
);
CREATE INDEX IF NOT EXISTS idx_risk_readings_ts ON risk_readings(ts);
CREATE INDEX IF NOT EXISTS idx_jobs_due ON jobs(status, run_at);
"""

_PREP_LOCK = threading.Lock()
_PREPARED: set[tuple[str, str]] = set()  # Postgres schemas already created and indexed in this process (a Store is built often)


MEMO_TTL = 5.0  # seconds: the longest a remembered answer is kept even when nothing in this process wrote. Another process (the CLI, an admin job) writes without telling this one; this bounds how stale that can make a read.
_GEN: dict[tuple[str, str], int] = {}
_MEMO: dict[tuple, tuple[tuple[int, ...], float, Any]] = {}
_MEMO_LOCK = threading.Lock()


_BACKGROUND = {"on": False}  # the running bot turns this on (serve_from_memory): a remembered answer whose time is up is still served, and re-read by the refresher thread, not by the caller
_REFRESH: "queue.Queue[tuple]" = queue.Queue()
_REFRESHING: set[tuple] = set()
_REFRESH_STORES = threading.local()


# ---- decision rows written off the evaluation's thread ----
# 2026-11-17, user: "why are you waiting on a database?"; "you shouldn't be using the db. everything in current scope for a buy/sell judgement comes from memory". Every evaluation ended by INSERTing its decision row and COMMITting it on the worker's one shared connection, so evaluations stood in line behind each other's writes (on the host at 09:58: a median 45 ms of database per evaluation, 427 ms at the 90th percentile, lock waits up to 591 ms; 99 of 100 evaluations are passes). In the running bot (queue_decisions) a row that is not a bet is handed to a writer thread, which inserts what has gathered and commits once. A bet's row is still written at once: the watcher and the books need it the moment the order is placed, and it comes after the judgement, not in it.
_DEC_Q: "queue.Queue[tuple[str, Any, dict]]" = queue.Queue()
_DEC_BG = {"on": False, "started": False}
_LAST_DEC: dict[tuple[str, str, Any], float] = {}  # (store path, ticker, lane or None for any lane) -> the time of the latest decision logged by this process
_LAST_DEC_LOCK = threading.Lock()
_PROC_START = time.time()
DEC_BATCH = 200


def queue_decisions(on: bool = True) -> None:
    """Turn the background decision writer on (the running bot) or off (tests, the CLI: rows are written at once, as they always were)."""
    _DEC_BG["on"] = on
    if on and not _DEC_BG["started"]:
        _DEC_BG["started"] = True
        threading.Thread(target=_decision_writer, name="decision-writer", daemon=True).start()


def writes_in_background() -> bool:
    return bool(_DEC_BG["on"])


def _with_snapshot(d: dict) -> dict:
    """Take the row's entry snapshot now, as of the row's own time (the caller handed over a job instead of taking it before the evaluation), and merge it under whatever factors the row already carries."""
    job = d.pop("_factors_job", None)
    if job is None:
        return d
    try:
        snap = job(float(d["ts"]))
        if snap:
            have = d.get("factors")
            d["factors"] = json.dumps(json.loads(snap) | json.loads(have)) if have else snap
    except Exception:
        _log.exception("decision writer: no entry snapshot for %s", d.get("ticker"))
    return d


def flush_decisions(timeout: float = 5.0) -> bool:
    """Wait until every queued decision row is written. True when the queue emptied in time."""
    end = time.monotonic() + timeout
    while _DEC_Q.unfinished_tasks and time.monotonic() < end:
        time.sleep(0.01)
    return not _DEC_Q.unfinished_tasks


def _decision_writer() -> None:
    stores: dict[tuple[str, Any], Store] = {}
    while True:
        batch = [_DEC_Q.get()]
        while len(batch) < DEC_BATCH:
            try:
                batch.append(_DEC_Q.get_nowait())
            except queue.Empty:
                break
        for attempt in range(5):
            try:
                touched = set()
                for path, exclude, d in batch:
                    _with_snapshot(d)  # once: the job is removed from the row, so a retry does not take it again
                    st = stores.get((path, exclude))
                    if st is None:
                        st = stores[(path, exclude)] = Store(path, exclude)
                    st.db.execute(f"INSERT INTO decisions ({','.join(d)}) VALUES ({','.join('?' for _ in d)})", list(d.values()))
                    touched.add((path, exclude))
                for key in touched:
                    stores[key].db.commit()
                break
            except Exception:
                _log.exception("decision writer: %d rows not written (try %d of 5)", len(batch), attempt + 1)
                stores.clear()  # a broken connection is not reused
                time.sleep(1.0)
        else:
            _log.error("decision writer: %d decision rows dropped after 5 tries", len(batch))
        for _ in batch:
            _DEC_Q.task_done()


def _note_decision(path: str, ticker: str, lane: Any, ts: float) -> None:
    with _LAST_DEC_LOCK:
        if len(_LAST_DEC) > 60000:  # closed markets pile up: keep the last two hours
            for k in [k for k, v in _LAST_DEC.items() if v < ts - 7200.0]:
                del _LAST_DEC[k]
        for key in ((path, ticker, lane), (path, ticker, None)):
            if _LAST_DEC.get(key, 0.0) < ts:
                _LAST_DEC[key] = ts


# ---- the bet book in memory ----
# 2026-11-17, user: "you shouldn't be using the db. everything in current scope for a buy/sell judgement comes from memory". An evaluation asked four things of the decisions table: the open bets, whether its lane already holds the market, which other markets of the event are bet, and when its lane last sold the market. Each was remembered per market and lane, but every bet, sale and settlement in the process made all of those answers stale at once, and the next evaluation of each market read its own again (with twenty lanes writing paper bets that was most evaluations: on the host at 10:50 every evaluation still spent a median 29 ms in the database, 480 ms at the 90th percentile).
# In the running bot all four are answered from one list held in memory: every bet row that is unsettled, or was sold in the last hour. The thread that WRITES a bet, a sale or a settlement rebuilds the list (one query, on its own connection, after its commit), so the list is exact the moment the write returns and no evaluation reads. A keeper thread rebuilds it every MEMO_TTL seconds for writes made by another process.
_BOOK: dict[str, tuple[int, list]] = {}  # store path -> (the "bets" generation it was read at, the rows)
_BOOK_LOCK = threading.Lock()
BOOK_KEEP_EXITS = 3600.0  # a sold bet stays in the list this long after its sale (the re-entry cooldown asks about it; the longest cooldown in use is 900 s)


def _lane_of(row: Any) -> str:
    return row["lane"] or "moonshot"


def book_open_bets(rows: Sequence[Any]) -> list:
    """Store.open_bets from the list: per market and lane, the first open bet; in time order."""
    first: dict[tuple[str, str], Any] = {}
    for r in rows:
        if r["outcome"] is None and r["exit_ts"] is None:
            k = (r["ticker"], _lane_of(r))
            if k not in first or r["id"] < first[k]["id"]:
                first[k] = r
    return sorted(first.values(), key=lambda r: r["ts"])


def book_holds_sized(rows: Sequence[Any], ticker: str, lane: str | None) -> bool:
    return any(r["ticker"] == ticker and r["stake_usd"] is not None and r["outcome"] is None and r["exit_ts"] is None and (lane is None or _lane_of(r) == lane) for r in rows)


def book_event_tickers(rows: Sequence[Any], event_ticker: str) -> set[str]:
    return {r["ticker"] for r in rows if r["event_ticker"] == event_ticker and r["outcome"] is None} if event_ticker else set()


def book_last_exit(rows: Sequence[Any], ticker: str, lane: str) -> float | None:
    got = [float(r["exit_ts"]) for r in rows if r["ticker"] == ticker and _lane_of(r) == lane and r["exit_ts"] is not None]
    return max(got) if got else None


def _book_keeper() -> None:
    stores: dict[str, Store] = {}
    while True:
        time.sleep(max(MEMO_TTL, 1.0))
        if not _BACKGROUND["on"]:
            continue
        for path in list(_BOOK):
            try:
                st = stores.get(path)
                if st is None:
                    st = stores[path] = Store(path)
                st.rebuild_book()
            except Exception:
                stores.pop(path, None)
                _log.exception("bet book: the keeper's rebuild failed; the last list stays in use")


def serve_from_memory(on: bool = True) -> None:
    """In the running bot (2026-11-16, user: "Nothing in per tick computations should do a db read. Ever"): a remembered read whose MEMO_TTL has run out is answered from memory and re-read by a background thread. The TTL exists only to pick up writes by another process; a write in THIS process still makes the next read go to the database, once, right after the bet, sale or settlement that changed the rows (the reader must not act on a book it knows is out of date)."""
    first = not _BACKGROUND["on"] and on
    _BACKGROUND["on"] = on
    if not on:
        _BOOK.clear()
    if first:
        threading.Thread(target=_refresher, name="store-memo", daemon=True).start()
        if not _BACKGROUND.get("keeper"):
            _BACKGROUND["keeper"] = True
            threading.Thread(target=_book_keeper, name="bet-book", daemon=True).start()


def _refresher() -> None:
    while True:
        key, fn, path, exclude, args, kw = _REFRESH.get()
        try:
            if not _BACKGROUND["on"]:
                continue
            stores = getattr(_REFRESH_STORES, "by_path", None)
            if stores is None:
                stores = _REFRESH_STORES.by_path = {}
            store = stores.get((path, exclude))
            if store is None:
                store = stores[(path, exclude)] = Store(path, exclude)
            gens = tuple(_GEN.get((path, d), 0) for d in fn.__memo_domains__)
            val = fn(store, *args, **dict(kw))
            with _MEMO_LOCK:
                _MEMO[key] = (gens, time.monotonic(), val)
        except Exception:
            _log.exception("store: background re-read of %s failed; the next caller reads it", fn.__name__)
            with _MEMO_LOCK:
                _MEMO.pop(key, None)
        finally:
            _REFRESHING.discard(key)


def _remembered(fn: Any) -> Any:
    """Answer a hot read from memory while nothing it depends on has been written.

    Each method names the domains it reads ("bets": the decisions rows; "orders": the orders rows). Every write through this module bumps its domains' generation (touch), so a remembered answer is used only while no
    write has happened in this process since it was read, and for at most MEMO_TTL seconds. The generation is read BEFORE the query, so a write that lands during it leaves the entry stale rather than fresh.
    In the running bot (serve_from_memory) an answer past its MEMO_TTL is still returned and the refresher thread re-reads it: the caller does not wait on the database."""
    domains: tuple[str, ...] = fn.__memo_domains__

    def wrapper(self: Store, *args: Any, **kw: Any) -> Any:
        key = (str(self.path), fn.__name__, args, tuple(sorted(kw.items())))
        gens = tuple(_GEN.get((str(self.path), d), 0) for d in domains)
        now = time.monotonic()
        hit = _MEMO.get(key)
        if hit is not None and hit[0] == gens and now - hit[1] < MEMO_TTL:
            val = hit[2]
        elif hit is not None and hit[0] == gens and _BACKGROUND["on"]:
            val = hit[2]  # nothing in this process wrote: serve it, and let the refresher look for another process's writes
            if key not in _REFRESHING:
                _REFRESHING.add(key)
                _REFRESH.put((key, fn, str(self.path), self.exclude_versions, args, tuple(sorted(kw.items()))))
        else:
            val = fn(self, *args, **kw)
            with _MEMO_LOCK:
                _MEMO[key] = (gens, now, val)
        return list(val) if isinstance(val, list) else set(val) if isinstance(val, set) else val

    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


def remembers(*domains: str) -> Any:
    def mark(fn: Any) -> Any:
        fn.__memo_domains__ = domains
        return _remembered(fn)

    return mark


class Store:
    def __init__(self, path: Path | str, exclude_versions: Iterable[str] = ()):
        self.path = Path(path)
        self.exclude_versions = frozenset(exclude_versions)
        self.db = db.connect(path, "main")
        key = (db.database_url(), db.schema_for(path, "main"))
        with _PREP_LOCK:  # two threads starting at once must not both create the tables
            first = not db.is_pg(self.db) or key not in _PREPARED
            if first and db.is_pg(self.db):
                _PREPARED.add(key)
        if first:
            self._drop_old_grid()
            self.db.executescript(SCHEMA)
            if not db.is_pg(self.db):
                self.db.execute("DROP TABLE IF EXISTS rag_fts")  # replaced by rag_cases
            self._migrate()
        self.generation = 0  # bumped whenever settled data changes; caches key on it
        self.rag = Rag(self, create=first)
        self.calibrator = Calibrator(self)
        from .bankroll import Bankroll  # local: bankroll imports this module

        self.bankroll = Bankroll(self)
        if first:
            self.rag.rebuild()

    def touch(self, *domains: str) -> None:
        """Tell the read memory that rows of these domains ("bets", "orders") changed. Every write in this module does it; code that writes with a raw statement on store.db must call it after the commit."""
        with _MEMO_LOCK:
            for d in domains:
                _GEN[(str(self.path), d)] = _GEN.get((str(self.path), d), 0) + 1
        if _BACKGROUND["on"] and "bets" in domains:  # the writer rebuilds the bet book, so no evaluation has to read after this write
            try:
                self.rebuild_book()
            except Exception:
                _BOOK.pop(str(self.path), None)  # no list is better than a wrong one: the next reader builds it
                _log.exception("bet book: the rebuild after a write failed")

    def rebuild_book(self) -> list:
        """Read the bet book (every bet row unsettled, or sold in the last hour) and put it in memory unless a newer one is already there."""
        p = str(self.path)
        gen = _GEN.get((p, "bets"), 0)
        # two reads, each on its own partial index (idx_decisions_bets_open, idx_decisions_bets_exit). As one statement with an OR it scanned the whole table: 155,127 rows for 16 on the host at 11:36 on 2026-11-17, 64 ms idle and about 600 ms on a busy bet
        rows = list(self.db.execute("SELECT * FROM decisions WHERE action='bet' AND outcome IS NULL").fetchall())
        rows += self.db.execute("SELECT * FROM decisions WHERE action='bet' AND exit_ts IS NOT NULL AND exit_ts > ? AND outcome IS NOT NULL", (time.time() - BOOK_KEEP_EXITS,)).fetchall()
        rows.sort(key=lambda r: (r["ts"], r["id"]))
        with _BOOK_LOCK:
            cur = _BOOK.get(p)
            if cur is None or cur[0] <= gen:
                _BOOK[p] = (gen, rows)
            return _BOOK[p][1]

    def _book(self) -> list | None:
        """The bet book when the running bot serves from memory, else None (the table is read, as it always was)."""
        if not _BACKGROUND["on"]:
            return None
        got = _BOOK.get(str(self.path))
        return got[1] if got is not None else self.rebuild_book()

    def _drop_old_grid(self) -> None:
        """The learner's tables gained the exit rule in their key. They are a cache of replays of the tape (grid.update rebuilds them), so an old shape is dropped."""
        cols = {r["name"] for r in self.db.execute("PRAGMA table_info(grid_obs)")}
        if cols and "rule" not in cols:
            self.db.execute("DROP TABLE grid_obs")
            self.db.execute("DROP TABLE IF EXISTS grid_done")
            self.db.commit()

    def _widen_counts(self) -> None:
        """Contract counts used to be whole numbers (BIGINT on Postgres); a fractional stake needs them to hold hundredths. SQLite stores either as it comes."""
        if not db.is_pg(self.db):
            return
        for table, col in (("decisions", "contracts"), ("orders", "count")):
            got = self.db.execute("SELECT data_type FROM information_schema.columns WHERE table_schema = current_schema() AND table_name = ? AND column_name = ?", (table, col)).fetchone()
            if got and got[0] == "bigint":
                self.db.execute(f"ALTER TABLE {table} ALTER COLUMN {col} TYPE DOUBLE PRECISION")
                self.db.commit()

    def _migrate(self) -> None:
        """Add columns to a database created before they existed."""
        self._widen_counts()
        have = {r["name"] for r in self.db.execute("PRAGMA table_info(decisions)")}
        for name, ddl in (
            ("lane", "TEXT DEFAULT 'moonshot'"),
            ("context", "TEXT"),
            ("category", "TEXT"),
            ("version", "TEXT"),
            ("event_ticker", "TEXT"),
            ("exit_price", "REAL"),
            ("exit_ts", "REAL"),
            ("exit_reason", "TEXT"),
            ("cal_p", "REAL"),
            ("side", "TEXT DEFAULT 'yes'"),
            ("contracts", "REAL"),
            ("stake_usd", "REAL"),
            ("lane_ver", "TEXT"),
            ("factors", "TEXT"),  # JSON snapshot of the entry context (factors.py)
            ("exit_factors", "TEXT"),  # JSON snapshot at the moment of the sale
        ):
            if name not in have:
                self.db.execute(f"ALTER TABLE decisions ADD COLUMN {name} {ddl}")
                if name == "version":
                    # Every row that exists before versions did was made by the old logic.
                    self.db.execute("UPDATE decisions SET version=?", (LEGACY_VERSION,))
        # the bet book's two reads (rebuild_book), made here and not in SCHEMA: an old database gains exit_ts in the loop above
        self.db.execute("CREATE INDEX IF NOT EXISTS idx_decisions_bets_open ON decisions(ts) WHERE action='bet' AND outcome IS NULL")
        self.db.execute("CREATE INDEX IF NOT EXISTS idx_decisions_bets_exit ON decisions(exit_ts) WHERE action='bet' AND exit_ts IS NOT NULL")
        candle_cols = {r["name"] for r in self.db.execute("PRAGMA table_info(candles)")}
        for col in ("bid_hi", "ask_lo"):
            if col not in candle_cols:
                self.db.execute(f"ALTER TABLE candles ADD COLUMN {col} REAL")
        window_cols = {r["name"] for r in self.db.execute("PRAGMA table_info(windows)")}
        if "settled_value" not in window_cols:  # 2026-11-11 (user: record each window's settled value): Kalshi's own series of 15-minute settlement values, kept beside the strike and result
            self.db.execute("ALTER TABLE windows ADD COLUMN settled_value REAL")
        job_cols = {r["name"] for r in self.db.execute("PRAGMA table_info(jobs)")}
        if "priority" not in job_cols:
            self.db.execute("ALTER TABLE jobs ADD COLUMN priority INTEGER NOT NULL DEFAULT 0")
        order_cols = {r["name"] for r in self.db.execute("PRAGMA table_info(orders)")}
        if "side" not in order_cols:
            self.db.execute("ALTER TABLE orders ADD COLUMN side TEXT DEFAULT 'buy'")
        if "contract" not in order_cols:
            self.db.execute("ALTER TABLE orders ADD COLUMN contract TEXT DEFAULT 'yes'")
        self.db.commit()

    def _snapshot(self) -> Path:
        """A copy made just before a deliberate change to the decisions: the whole file for SQLite, a portable copy of the table for Postgres."""
        dest = self.path.with_name(f"{self.path.name}.bak-{int(time.time())}")
        if not db.is_pg(self.db):
            shutil.copy2(self.path, dest)
            return dest
        cols = [r["name"] for r in self.db.execute("PRAGMA table_info(decisions)")]
        out = sqlite3.connect(dest)
        out.execute(f"CREATE TABLE decisions ({', '.join(cols)})")
        out.executemany(f"INSERT INTO decisions VALUES ({','.join('?' * len(cols))})", [tuple(r) for r in self.db.execute("SELECT * FROM decisions")])
        out.commit()
        out.close()
        return dest

    def _excluded_sql(self) -> tuple[str, list[str]]:
        if not self.exclude_versions:
            return "", []
        marks = ",".join("?" for _ in self.exclude_versions)
        return f" AND COALESCE(version,'{LEGACY_VERSION}') NOT IN ({marks})", sorted(self.exclude_versions)

    def version_counts(self) -> list[db.Row]:
        """Per version: decisions logged and how many of those have settled."""
        return self.db.execute(
            f"""SELECT COALESCE(version,'{LEGACY_VERSION}') AS version, COUNT(*) AS decisions,
                       SUM(CASE WHEN outcome IN ('yes','no') THEN 1 ELSE 0 END) AS settled
                FROM decisions GROUP BY 1 ORDER BY 1"""
        ).fetchall()

    def uncategorized_series(self) -> dict[str, int]:
        """Series of decisions with no category, with how many decisions each has."""
        rows = self.db.execute(
            """SELECT ticker FROM decisions WHERE category IS NULL OR category = ''"""
        ).fetchall()
        out: dict[str, int] = {}
        for r in rows:
            series = r["ticker"].split("-")[0]
            out[series] = out.get(series, 0) + 1
        return out

    def backfill_categories(self, categories: dict[str, str], dry_run: bool = True) -> dict[str, Any]:
        """Fill in the category of old decisions from a series -> category map.

        Only rows with an empty category are touched, and only their category column. Makes a
        copy of the database file first when actually writing. Rebuilds the similar-case index
        and invalidates calibration caches, since both use the category.
        """
        pending = self.uncategorized_series()
        fill = {s: c for s, c in categories.items() if c and s in pending}
        out: dict[str, Any] = {
            "rows": sum(pending.values()),
            "fillable": sum(pending[s] for s in fill),
            "unresolved": sorted(set(pending) - set(fill)),
            "updated": 0,
        }
        if dry_run or not fill:
            return out
        backup = self._snapshot()
        for series, category in fill.items():
            cur = self.db.execute(
                """UPDATE decisions SET category=?
                   WHERE (category IS NULL OR category='') AND (ticker=? OR ticker LIKE ?)""",
                (category, series, series + "-%"),
            )
            out["updated"] += cur.rowcount
        self.db.commit()
        self.generation += 1
        self.rebuild_rag()
        out["backup"] = str(backup)
        return out

    def prune(self, versions: Iterable[str], dry_run: bool = True) -> dict[str, int]:
        """Delete decisions of the given versions, except tickers we have orders on.

        Orders are never touched, and a ticker with an order keeps its decisions: the open
        position check reads settled decisions, so deleting them would re-open a settled position.
        Makes a copy of the database file first when actually deleting.
        """
        versions = sorted(set(versions))
        if not versions:
            return {"matched": 0, "kept_for_orders": 0, "deleted": 0}
        marks = ",".join("?" for _ in versions)
        where = (
            f"COALESCE(version,'{LEGACY_VERSION}') IN ({marks})"
            " AND ticker NOT IN (SELECT ticker FROM orders)"
        )
        matched = self.db.execute(
            f"SELECT COUNT(*) FROM decisions WHERE COALESCE(version,'{LEGACY_VERSION}') IN ({marks})",
            versions,
        ).fetchone()[0]
        deletable = self.db.execute(f"SELECT COUNT(*) FROM decisions WHERE {where}", versions).fetchone()[0]
        out = {"matched": matched, "kept_for_orders": matched - deletable, "deleted": 0}
        if dry_run or not deletable:
            return out
        backup = self._snapshot()
        self.db.execute(f"DELETE FROM decisions WHERE {where}", versions)
        self.db.commit()
        self.touch("bets", "orders")
        self.generation += 1
        self.rebuild_rag()
        out["deleted"] = deletable
        out["backup"] = str(backup)
        return out

    # ---- decisions ----
    def add_decision(self, **d: Any) -> int:
        d.setdefault("ts", time.time())
        d.setdefault("version", STRATEGY_VERSION)
        _note_decision(str(self.path), d["ticker"], d.get("lane"), float(d["ts"]))
        if _DEC_BG["on"] and d.get("action") != "bet":
            _DEC_Q.put((str(self.path), self.exclude_versions, d))  # written by the decision-writer thread: the evaluation does not wait on the database
            return 0
        _with_snapshot(d)  # a bet's row, written here and now: its snapshot is taken after the judgement and the order, not before
        cols = ",".join(d)
        marks = ",".join("?" for _ in d)
        cur = self.db.execute(f"INSERT INTO decisions ({cols}) VALUES ({marks})", list(d.values()))
        self.db.commit()
        if d.get("action") == "bet":
            self.touch("bets")
        return int(cur.lastrowid or 0)

    def add_risk_readings(self, rows: Sequence[tuple[str, float, str, float | None, float | None, float]]) -> None:
        """Every open market's risk reading at a 30 s mark (riskrecord.py's background thread; never an evaluation). A mark already written is left."""
        for r in rows:
            self.db.execute("INSERT OR IGNORE INTO risk_readings (ticker, ts, x, yes_bid, yes_ask, age) VALUES (?,?,?,?,?,?)", tuple(r))
        if rows:
            self.db.commit()

    def add_manual_entry(self, **e: Any) -> None:
        """Record a hand-placed buy and the entry context at the moment of the fill. A repeated fill id is ignored."""
        cols = ",".join(e)
        self.db.execute(f"INSERT OR IGNORE INTO manual_entries ({cols}) VALUES ({','.join('?' for _ in e)})", list(e.values()))
        self.db.commit()

    def mark_template(self, ticker: str, note: str = "", undo: bool = False) -> int:
        """Mark (or with undo, unmark) the hand-placed buys logged for this market as templates. Returns how many rows changed."""
        cur = self.db.execute("UPDATE manual_entries SET template=?, note=? WHERE ticker=?", (0 if undo else 1, "" if undo else note, ticker))
        self.db.commit()
        return int(cur.rowcount or 0)

    def is_bot_order(self, order_id: str) -> bool:
        return bool(order_id) and self.db.execute("SELECT 1 FROM orders WHERE order_id=? LIMIT 1", (order_id,)).fetchone() is not None

    def paper_bets(self, open_only: bool, live: bool = False) -> list[db.Row]:
        """Sized bets (one per market, the first). open_only: still riding; else: finished.

        live=False: paper bets, those made while their lane was not placing orders.
        live=True: bets that were sent as real orders.
        Hand-placed buys (lane manual) are logged for study and left out of both.

        """
        done = "(COALESCE(outcome,'') IN ('yes','no') OR exit_ts IS NOT NULL)"
        return self.db.execute(
            f"""SELECT id, ticker, lane, side, outcome, contracts, stake_usd, exit_price
                FROM decisions
                WHERE action='bet' AND stake_usd IS NOT NULL AND contracts > 0
                  AND COALESCE(lane,'moonshot') != 'manual'
                  AND (CASE WHEN mode = 'trade' THEN 1 ELSE 0 END) = ?
                  AND {'NOT ' + done if open_only else done}""",
            (1 if live else 0,),
        ).fetchall()

    def holds_sized_bet(self, ticker: str, lane: str | None = None) -> bool:
        """A sized bet on this market is already open (so a re-score must not stake it again).

        With `lane`, only that lane's bet counts: two lanes (an A/B pair, say) can each hold the same market. From the bet book in the running bot.
        """
        book = self._book()
        return book_holds_sized(book, ticker, lane) if book is not None else self._holds_sized_bet_read(ticker, lane)

    @remembers('bets')
    def _holds_sized_bet_read(self, ticker: str, lane: str | None = None) -> bool:
        row = self.db.execute(
            """SELECT 1 FROM decisions WHERE ticker=? AND action='bet' AND stake_usd IS NOT NULL
               AND outcome IS NULL AND exit_ts IS NULL AND (? IS NULL OR COALESCE(lane,'moonshot')=?) LIMIT 1""",
            (ticker, lane, lane),
        ).fetchone()
        return row is not None

    # ---- cash the bot moved between your exchanges (see runner._top_up) ----
    def add_shard_move(self, src: int, dst: int, usd: float, reason: str, transfer_id: str = "") -> None:
        self.db.execute("INSERT INTO shard_moves (ts, src_shard, dst_shard, usd, reason, transfer_id) VALUES (?,?,?,?,?,?)",
                        (time.time(), src, dst, usd, reason[:200], transfer_id))
        self.db.commit()

    def shard_moved_since(self, since_ts: float) -> float:
        row = self.db.execute("SELECT COALESCE(SUM(usd),0) FROM shard_moves WHERE ts>=?", (since_ts,)).fetchone()
        return float(row[0])

    # ---- screened-out candidates: markets that passed the lane's filter but never got a decision ----
    def log_screened(self, lane: str, rows: list[dict[str, Any]], now: float | None = None, gap: float = 300.0) -> int:
        """Record why candidates were dropped by the estimator's screen. One row per ticker and side per `gap` seconds."""
        now = time.time() if now is None else now
        n = 0
        for r in rows:
            seen = self.db.execute(
                "SELECT 1 FROM screen_log WHERE lane=? AND ticker=? AND side=? AND ts>=? LIMIT 1",
                (lane, r["ticker"], r["side"], now - gap),
            ).fetchone()
            if seen:
                continue
            self.db.execute(
                "INSERT INTO screen_log (ts, lane, ticker, side, ask, model_p, edge, reason) VALUES (?,?,?,?,?,?,?,?)",
                (now, lane, r["ticker"], r["side"], r.get("ask"), r.get("p"), r.get("edge"), r.get("reason")),
            )
            n += 1
        self.db.commit()
        return n

    def prune_screened(self, days: float = 3.0, now: float | None = None) -> None:
        now = time.time() if now is None else now
        self.db.execute("DELETE FROM screen_log WHERE ts<?", (now - days * 86400,))
        self.db.commit()

    def decided_since(self, ticker: str, since_ts: float, lane: str | None = None) -> bool:
        """Whether a decision on the market was logged since `since_ts`: by any lane, or (with `lane`) by that lane alone. The review cooldown asks with the lane's own name (2026-11-15): the paper variants log hundreds of decisions an hour on the same markets, and counted from every lane they kept the live lane's review off 57% of the last two hours."""
        if _DEC_BG["on"]:  # the running bot: rows are queued, so memory is asked first (outside it the table is read, as it always was)
            last = _LAST_DEC.get((str(self.path), ticker, lane))
            if last is not None and last >= since_ts:
                return True  # this process logged one; its row may still be in the queue
            if since_ts >= _PROC_START:
                return False  # every decision since the bot started passed through add_decision: memory is the whole answer
        if lane is None:
            row = self.db.execute("SELECT 1 FROM decisions WHERE ticker=? AND ts>=? LIMIT 1", (ticker, since_ts)).fetchone()
        else:
            row = self.db.execute("SELECT 1 FROM decisions WHERE ticker=? AND ts>=? AND lane=? LIMIT 1", (ticker, since_ts, lane)).fetchone()
        return row is not None

    # ---- watchlist: what the slow scoping pass hands to the fast review pass ----
    def set_watch(self, lane: str, events: dict[str, dict[str, Any]]) -> None:
        """Replace a lane's watchlist."""
        now = time.time()
        self.db.execute("DELETE FROM watchlist WHERE lane=?", (lane,))
        self.db.executemany(
            "INSERT INTO watchlist (lane, event_ticker, payload, scoped_ts) VALUES (?,?,?,?)",
            [(lane, ev, json.dumps(info), now) for ev, info in events.items()],
        )
        self.db.commit()

    def watch(self, lane: str) -> dict[str, dict[str, Any]]:
        rows = self.db.execute(
            "SELECT event_ticker, payload FROM watchlist WHERE lane=?", (lane,)
        ).fetchall()
        return {r["event_ticker"]: json.loads(r["payload"] or "{}") for r in rows}

    def unwatch(self, lane: str, event_ticker: str) -> None:
        self.db.execute(
            "DELETE FROM watchlist WHERE lane=? AND event_ticker=?", (lane, event_ticker)
        )
        self.db.commit()

    def decision_lane(self, ticker: str) -> str:
        """The lane of the newest unsettled decision on this ticker, or "" when there is none."""
        row = self.db.execute(
            "SELECT lane FROM decisions WHERE ticker=? AND outcome IS NULL ORDER BY ts DESC LIMIT 1",
            (ticker,),
        ).fetchone()
        return (row["lane"] if row else "") or ""

    def unresolved_tickers(self) -> list[str]:
        rows = self.db.execute(
            """SELECT ticker FROM decisions WHERE outcome IS NULL
               UNION SELECT ticker FROM shadow WHERE outcome IS NULL"""
        ).fetchall()
        return [r["ticker"] for r in rows]

    # ---- shadow log: prices of markets we only watch, to measure them against outcomes ----
    def add_shadow(self, **row: Any) -> None:
        row.setdefault("ts", time.time())
        cols = ",".join(row)
        marks = ",".join("?" for _ in row)
        self.db.execute(f"INSERT INTO shadow ({cols}) VALUES ({marks})", list(row.values()))
        self.db.commit()

    def shadowed_since(self, ticker: str, since_ts: float) -> bool:
        return self.db.execute(
            "SELECT 1 FROM shadow WHERE ticker=? AND ts>=? LIMIT 1", (ticker, since_ts)
        ).fetchone() is not None

    def shadow_rows(self, settled_only: bool = True) -> list[db.Row]:
        where = "WHERE outcome IN ('yes','no')" if settled_only else ""
        return self.db.execute(f"SELECT * FROM shadow {where} ORDER BY ts").fetchall()

    def set_outcome(self, ticker: str, outcome: str) -> None:
        self.db.execute(
            "UPDATE decisions SET outcome=?, settled_ts=? WHERE ticker=? AND outcome IS NULL",
            (outcome, time.time(), ticker),
        )
        self.db.execute(
            "UPDATE shadow SET outcome=?, settled_ts=? WHERE ticker=? AND outcome IS NULL",
            (outcome, time.time(), ticker),
        )
        self.db.commit()
        self.touch("bets", "orders")
        self.generation += 1
        self.rag.index_settled([ticker])

    def settled_decisions(self, include_excluded: bool = False, tickers: Iterable[str] | None = None) -> list[db.Row]:
        """Settled decisions, minus excluded versions unless asked for everything. `tickers` narrows the read to those markets: the case index asks for one market at each settlement, and without this it loaded every settled decision each time (132,508 rows on 2026-11-16, on three connections at once)."""
        extra, args = ("", []) if include_excluded else self._excluded_sql()
        if tickers is not None:
            wanted = sorted(set(tickers))
            if not wanted:
                return []
            extra += f" AND ticker IN ({','.join('?' for _ in wanted)})"
            args = list(args) + wanted
        return self.db.execute(
            f"SELECT * FROM decisions WHERE outcome IN ('yes','no'){extra} ORDER BY ts", args
        ).fetchall()

    def settled_for_record(self) -> list[db.Row]:
        """The settled decisions as the record adjustment needs them: seven short columns, no notes or contexts. settled_decisions drags every column (127 MB on 2026-11-16); building Python rows from that held every other thread of the bot for seconds."""
        extra, args = self._excluded_sql()
        return self.db.execute(
            f"SELECT lane, ticker, model_p, ask, side, outcome, category FROM decisions WHERE outcome IN ('yes','no') AND model_p IS NOT NULL{extra} ORDER BY ts", args
        ).fetchall()

    def bet_tickers_in_event(self, event_ticker: str) -> set[str]:
        """Unresolved paper or real bets in this event. Empty when event_ticker is blank. From the bet book in the running bot."""
        book = self._book()
        return book_event_tickers(book, event_ticker) if book is not None else self._bet_tickers_in_event_read(event_ticker)

    @remembers('bets')
    def _bet_tickers_in_event_read(self, event_ticker: str) -> set[str]:
        if not event_ticker:
            return set()
        rows = self.db.execute(
            """SELECT DISTINCT ticker FROM decisions
               WHERE event_ticker=? AND action='bet' AND outcome IS NULL""",
            (event_ticker,),
        ).fetchall()
        return {r["ticker"] for r in rows}

    def recent_decisions(self, limit: int = 20) -> list[db.Row]:
        return self.db.execute(
            "SELECT * FROM decisions ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()

    # ---- orders ----
    def add_order(self, **o: Any) -> None:
        o.setdefault("ts", time.time())
        cols = ",".join(o)
        marks = ",".join("?" for _ in o)
        self.db.execute(f"INSERT INTO orders ({cols}) VALUES ({marks})", list(o.values()))
        self.db.commit()
        self.touch("orders")

    @remembers('orders')
    def last_order_ts(self, ticker: str, env: str) -> float:
        """When the bot last placed an order on this market (0 if never): a positions read can trail a fill by a few seconds."""
        row = self.db.execute("SELECT COALESCE(MAX(ts), 0) AS t FROM orders WHERE ticker=? AND env=?", (ticker, env)).fetchone()
        return float(row["t"])

    def net_contracts(self, ticker: str, env: str) -> float:
        row = self.db.execute(
            """SELECT COALESCE(SUM(CASE WHEN COALESCE(side,'buy')='buy' THEN fill_count
                                        ELSE -fill_count END), 0) AS n
               FROM orders WHERE ticker=? AND env=?""",
            (ticker, env),
        ).fetchone()
        return float(row["n"])

    # ---- resting sells: a take-profit sell resting on the book (runner._rest_take_profit) ----
    # Placed as an orders row with side='rest' and fill_count 0 (it holds nothing yet); each fill is booked as an
    # ordinary side='sell' row under the same order_id. Retiring one (cancelled, or given up on) zeroes its count.

    @remembers('orders')
    def rest_sell(self, ticker: str, env: str) -> db.Row | None:
        """The latest resting sell placed on this ticker, working or not."""
        return self.db.execute(
            "SELECT * FROM orders WHERE ticker=? AND env=? AND side='rest' ORDER BY id DESC LIMIT 1", (ticker, env)
        ).fetchone()

    @remembers('orders')
    def booked_rest_sell(self, order_id: str) -> tuple[float, float]:
        """Contracts of a resting sell already booked as sold, and their average fill price."""
        row = self.db.execute(
            "SELECT COALESCE(SUM(fill_count), 0) AS n, COALESCE(SUM(price * fill_count), 0) AS pv FROM orders WHERE order_id=? AND side='sell'",
            (order_id,),
        ).fetchone()
        n = float(row["n"])
        return n, (float(row["pv"]) / n if n > 0 else 0.0)

    def working_rest_sell(self, ticker: str, env: str) -> db.Row | None:
        """The resting sell still working on this ticker: placed, not retired, and not yet fully filled."""
        row = self.rest_sell(ticker, env)
        if row is None or float(row["count"]) <= 0:
            return None
        booked, _ = self.booked_rest_sell(row["order_id"])
        return row if booked < float(row["count"]) - 1e-9 else None

    def retire_rest_sell(self, order_id: str) -> None:
        """Mark a resting sell no longer working, so nothing keeps waiting for it or stands down for it."""
        self.db.execute("UPDATE orders SET count=0 WHERE order_id=? AND side='rest'", (order_id,))
        self.db.commit()
        self.touch("orders")

    def net_cost(self, ticker: str, env: str) -> float:
        """Dollars the bot's own orders have put into this ticker (buys cost, sales return), fees included in the order cost."""
        row = self.db.execute("SELECT COALESCE(SUM(cost_usd + CASE WHEN COALESCE(side,'buy')='buy' THEN COALESCE(fee_usd, 0) ELSE 0 END), 0) AS c FROM orders WHERE ticker=? AND env=?", (ticker, env)).fetchone()
        return float(row["c"])

    def note_lane_version(self, lane: str, ver: str, settings: dict[str, str]) -> None:
        """Record the first time a version of a lane is used, with its settings, so the changes between versions can be read back."""
        self.db.execute("INSERT OR IGNORE INTO lane_versions (lane, ver, first_ts, settings) VALUES (?,?,?,?)", (lane, ver, time.time(), json.dumps(settings)))
        self.db.commit()

    def lane_versions(self, lane: str) -> list[db.Row]:
        return self.db.execute("SELECT ver, first_ts, settings FROM lane_versions WHERE lane=? ORDER BY first_ts", (lane,)).fetchall()

    def note_window(self, ticker: str, close_ts: int, strike: float | None = None, result: str | None = None) -> None:
        """Keep what the stream says of a 15-minute window: its strike when it opens, its result when it settles. The two arrive apart, so each fills only its own column. The window offset is learned from these (commodities.learn_windows), so no REST read of settled markets is needed."""
        if strike is None and result not in ("yes", "no"):
            return
        row = self.db.execute("SELECT strike, result FROM window_marks WHERE ticker=?", (ticker,)).fetchone()
        have_strike, have_result = (row["strike"], row["result"]) if row else (None, None)
        self.db.execute(
            "INSERT OR REPLACE INTO window_marks (ticker, series, close_ts, strike, result, noted_ts) VALUES (?,?,?,?,?,?)",
            (ticker, ticker.split("-")[0], int(close_ts), strike or have_strike, result if result in ("yes", "no") else have_result, time.time()),
        )
        self.db.commit()

    def settled_windows(self, series: str, limit: int = 40) -> list[dict[str, Any]]:
        """The series' newest windows in the shape Kalshi's settled-markets listing has (ticker, close_time, floor_strike, result, expiration_value), from the stream's marks and the history job's windows. Where both hold a window the history row wins. A window with no strike is left out."""
        from datetime import UTC, datetime

        out: dict[str, dict[str, Any]] = {}
        for t, c, k, r, v in self.db.execute(
            "SELECT ticker, close_ts, strike, result, NULL FROM window_marks WHERE series=? AND strike IS NOT NULL ORDER BY close_ts DESC LIMIT ?", (series, limit),
        ).fetchall():
            out[t] = {"ticker": t, "close_ts": c, "floor_strike": k, "result": r, "expiration_value": v}
        for t, c, k, r, v in self.db.execute(
            "SELECT ticker, close_ts, strike, result, settled_value FROM windows WHERE series=? AND strike IS NOT NULL ORDER BY close_ts DESC LIMIT ?", (series, limit),
        ).fetchall():
            out[t] = {"ticker": t, "close_ts": c, "floor_strike": k, "result": r, "expiration_value": v}
        rows = sorted(out.values(), key=lambda m: -m["close_ts"])[:limit]
        for m in rows:
            m["close_time"] = datetime.fromtimestamp(m.pop("close_ts"), UTC).isoformat()
        return rows

    def settled_markets(self, series: str, limit: int = 40) -> list[dict[str, Any]]:
        """The name the window learner calls on its source (a Kalshi client has the same method): this store answers from its own rows."""
        return self.settled_windows(series, limit)

    @remembers()
    def window_strike(self, ticker: str) -> float | None:
        """The strike of a market's window (None until the window row exists). A strike never changes, so this is remembered for the memory's TTL whatever else is written."""
        row = self.db.execute("SELECT strike FROM windows WHERE ticker=?", (ticker,)).fetchone()
        return float(row["strike"]) if row and row["strike"] else None

    def _last_exit(self, ticker: str, lane: str) -> float | None:
        book = self._book()
        return book_last_exit(book, ticker, lane) if book is not None else self._last_exit_read(ticker, lane)

    @remembers("bets")
    def _last_exit_read(self, ticker: str, lane: str) -> float | None:
        row = self.db.execute(
            """SELECT MAX(exit_ts) AS t FROM decisions
               WHERE ticker=? AND COALESCE(lane,'moonshot')=? AND action='bet' AND exit_ts IS NOT NULL""",
            (ticker, lane),
        ).fetchone()
        return float(row["t"]) if row and row["t"] is not None else None

    def exited_within(self, ticker: str, lane: str, seconds: float, now: float | None = None) -> float:
        """Seconds still to wait if this lane sold this market less than `seconds` ago, else 0."""
        now = time.time() if now is None else now
        last = self._last_exit(ticker, lane)
        return max(0.0, last + seconds - now) if last is not None else 0.0

    def entry_fill(self, ticker: str, side: str, since_ts: float) -> float | None:
        """The price per contract a position actually filled at, fee excluded (what the exits compare a bid with), from the buy orders on this market and contract from since_ts (a few seconds before the bet) on: (cost_usd - fee_usd) over fill_count, a top-up counted. None when there are no filled buy orders (a paper bet): the caller keeps the decision's ask. The decision's ask is not this: a take chases the ask by the lane's slip, and a resting bid fills under it."""
        row = self.db.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) AS cost, COALESCE(SUM(fee_usd), 0) AS fee, COALESCE(SUM(fill_count), 0) AS n FROM orders "
            "WHERE ticker=? AND COALESCE(contract,'yes')=? AND COALESCE(side,'buy')='buy' AND ts>=?",
            (ticker, side or "yes", float(since_ts) - 5.0),
        ).fetchone()
        if not row or float(row["n"]) <= 0:
            return None
        price = (float(row["cost"]) - float(row["fee"])) / float(row["n"])
        return price if 0.0 < price < 1.0 else None

    def last_gain_sale(self, ticker: str, lane: str) -> tuple[str, float] | None:
        """(side, sale time) of this lane's latest sale of this market for a gain (the sale after its fee over what the position cost, fee included), else None. A sale at a loss is not counted. The cost is what the buy orders in the position's life actually filled at (orders: cost_usd over fill_count, resting bids fill under the ask), and falls back to the decision's ask plus its fee when there are no orders (a paper bet)."""
        from .strategy import fee_per_contract as fee

        book = self._book()
        if book is not None:  # the running bot: the lane's sales of this market are in the bet book (a sold bet stays in it for an hour); with none, nothing is read at all
            rows = sorted((r for r in book if r["ticker"] == ticker and _lane_of(r) == lane and r["exit_ts"] is not None and r["exit_price"] is not None and r["ask"] is not None), key=lambda r: -float(r["exit_ts"]))
        else:
            rows = self.db.execute(
                """SELECT ts, side, exit_price, exit_ts, ask FROM decisions
                   WHERE ticker=? AND COALESCE(lane,'moonshot')=? AND action='bet' AND exit_ts IS NOT NULL AND exit_price IS NOT NULL AND ask IS NOT NULL
                   ORDER BY exit_ts DESC""",
                (ticker, lane),
            ).fetchall()
        for r in rows:
            side = r["side"] or "yes"
            sold, paid = float(r["exit_price"]), float(r["ask"])
            filled = self.db.execute(
                "SELECT COALESCE(SUM(cost_usd), 0) AS cost, COALESCE(SUM(fill_count), 0) AS n FROM orders "
                "WHERE ticker=? AND COALESCE(contract,'yes')=? AND COALESCE(side,'buy')='buy' AND ts>=? AND ts<=?",
                (ticker, side, float(r["ts"]) - 5.0, float(r["exit_ts"])),
            ).fetchone()
            cost = float(filled["cost"]) / float(filled["n"]) if filled and float(filled["n"]) > 0 else paid + fee(paid)
            if sold - fee(sold) > cost:
                return side, float(r["exit_ts"])
        return None

    def mark_exited(self, ticker: str, price: float, reason: str, lane: str | None = None, factors: str = "", side: str | None = None) -> None:
        """Write a sale on the lane's open bets on this market. With `side`, only the bets on that side: a lane may have an open row on each side of one market (a paper lane logs the other side unstaked while it holds the first), and one side's sale price is not the other's. 2026-11-18: HYPE 03:30, the NO bet's sale at 0.17 was written on the YES row too, bought 40 s earlier at 0.79. The other side's row stays open and gets its own exit or its settlement."""
        self.db.execute(
            """UPDATE decisions SET exit_price=?, exit_ts=?, exit_reason=?, exit_factors=?
               WHERE ticker=? AND action='bet' AND outcome IS NULL AND exit_ts IS NULL
                 AND (? IS NULL OR COALESCE(lane,'moonshot')=?)
                 AND (? IS NULL OR COALESCE(side,'yes')=?)""",
            (price, time.time(), reason[:900], factors or None, ticker, lane, lane, side, side),  # 900, not 300 (2026-11-14): a profit take's reason carries the typical minute and the cushion note at its end, which the cut dropped
        )
        self.db.commit()
        self.touch("bets")

    def open_bets(self) -> list[db.Row]:
        """Unresolved bets not yet stopped out. One row per ticker and lane (that lane's first bet). From the bet book in the running bot."""
        book = self._book()
        return book_open_bets(book) if book is not None else self._open_bets_read()

    @remembers('bets')
    def _open_bets_read(self) -> list[db.Row]:
        return self.db.execute(
            """SELECT d.* FROM decisions d
               JOIN (
                 SELECT ticker, COALESCE(lane,'moonshot') AS lane, MIN(id) AS id FROM decisions
                 WHERE action='bet' AND outcome IS NULL AND exit_ts IS NULL
                 GROUP BY ticker, COALESCE(lane,'moonshot')
               ) fst ON d.id = fst.id
               ORDER BY d.ts"""
        ).fetchall()

    def open_positions(self, env: str) -> list[db.Row]:
        """Real open buys, with the bet decision logged at the first fill.

        A later re-score of the same ticker is left out. That row is a new estimate,
        not the one that placed the order.
        """
        return self.db.execute(
            """SELECT p.ticker, p.contracts, p.cost_usd, p.price, p.filled_ts,
                      d.title, d.ask, d.model_p, d.edge, d.context
               FROM (
                 SELECT ticker,
                        SUM(CASE WHEN COALESCE(side,'buy')='buy' THEN fill_count
                                 ELSE -fill_count END) AS contracts,
                        SUM(CASE WHEN COALESCE(side,'buy')='buy' THEN cost_usd
                                 ELSE 0 END) AS cost_usd,
                        MIN(CASE WHEN COALESCE(side,'buy')='buy' THEN price END) AS price,
                        MIN(CASE WHEN COALESCE(side,'buy')='buy' THEN ts END) AS filled_ts
                 FROM orders WHERE env=?
                 GROUP BY ticker
                 HAVING SUM(CASE WHEN COALESCE(side,'buy')='buy' THEN fill_count
                                 ELSE -fill_count END) > 0
               ) p
               LEFT JOIN decisions d ON d.id = (
                 SELECT id FROM decisions
                 WHERE ticker=p.ticker AND action='bet' AND ts <= p.filled_ts + 5
                 ORDER BY ts DESC LIMIT 1
               )
               WHERE NOT EXISTS (
                 SELECT 1 FROM decisions x
                 WHERE x.ticker=p.ticker AND x.outcome IN ('yes','no')
               )
               AND NOT EXISTS (
                 SELECT 1 FROM decisions x
                 WHERE x.ticker=p.ticker AND x.exit_ts IS NOT NULL
               )
               ORDER BY p.filled_ts""",
            (env,),
        ).fetchall()

    @remembers('bets', 'orders')
    def open_position_tickers(self, env: str) -> set[str]:
        """Tickers we still hold, from the orders themselves: more bought than sold, and the market not settled.

        A bet marked as exited does not matter here: if the orders still show contracts held (a second bet bought after the
        first exit), the ticker is held, so no lane buys it twice.
        """
        rows = self.db.execute(
            """SELECT o.ticker FROM orders o
               WHERE o.env=?
               GROUP BY o.ticker
               HAVING SUM(CASE WHEN COALESCE(o.side,'buy')='buy' THEN o.fill_count
                               ELSE -o.fill_count END) > 0
                 AND NOT EXISTS (SELECT 1 FROM decisions d
                                 WHERE d.ticker=o.ticker AND d.outcome IN ('yes','no'))""",
            (env,),
        ).fetchall()
        return {r["ticker"] for r in rows}

    # ---- local retrieval of settled decisions (no prices); see rag.py ----
    def rebuild_rag(self) -> int:
        """Rebuild the whole index. Needed when excluded versions or skipped lanes change."""
        return self.rag.rebuild()

    def similar_text(self, title: str, rules: str = "", limit: int = 4) -> str:
        """Nearest settled cases for a new question. Empty when nothing useful matches."""
        return "\n".join(
            c.line()
            for c in self.rag.cases(f"{title} {rules}", limit=limit)
        )
