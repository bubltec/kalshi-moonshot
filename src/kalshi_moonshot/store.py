"""The bot's database (Postgres in production, SQLite for tests; see db.py). Every decision is logged so the model can be scored later.

Settled decisions are also indexed in a local full-text table. The next estimate can
retrieve similar past cases. Prices are not stored in that index.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import threading
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from . import db
from .calibration import Calibrator
from .rag import Rag

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

CREATE TABLE IF NOT EXISTS esports_forecasts (
  event_ticker TEXT PRIMARY KEY, -- one forecast per match (esports.py)
  ticker TEXT NOT NULL,          -- the team market that was asked about
  game TEXT, team TEXT, rival TEXT,
  ts REAL NOT NULL, start_ts REAL,
  p_model REAL NOT NULL,         -- the model's chance that `team` wins, from news only (it never sees the price)
  why TEXT, news TEXT,
  bid REAL, ask REAL, mid REAL,  -- the market when the forecast was made
  outcome TEXT, settled_ts REAL, -- 'yes' / 'no' once the match is over
  p_rank REAL,                   -- the same question with both teams' rankings added to the news (rankings.py); empty when no ranking could be read
  rank_why TEXT, ranking TEXT    -- what the model said, and the ranking lines it was shown
);

CREATE TABLE IF NOT EXISTS flips (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  ticker TEXT NOT NULL,
  stopped_side TEXT NOT NULL,    -- the side that was sold at a stop; the flip considers the other one
  lane TEXT,                     -- the lane that held it ('manual' for a hand-placed buy)
  stake_usd REAL,
  status TEXT NOT NULL DEFAULT 'pending',  -- pending | done | expired
  note TEXT
);
CREATE INDEX IF NOT EXISTS idx_flips ON flips(status, ts);

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
  fetched_ts REAL NOT NULL
);

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
CREATE INDEX IF NOT EXISTS idx_jobs_due ON jobs(status, run_at);
"""

_PREP_LOCK = threading.Lock()
_PREPARED: set[tuple[str, str]] = set()  # Postgres schemas already created and indexed in this process (a Store is built often)


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
        have_es = {r["name"] for r in self.db.execute("PRAGMA table_info(esports_forecasts)")}
        for name, ddl in (("p_rank", "REAL"), ("rank_why", "TEXT"), ("ranking", "TEXT")):
            if name not in have_es:
                self.db.execute(f"ALTER TABLE esports_forecasts ADD COLUMN {name} {ddl}")
        candle_cols = {r["name"] for r in self.db.execute("PRAGMA table_info(candles)")}
        for col in ("bid_hi", "ask_lo"):
            if col not in candle_cols:
                self.db.execute(f"ALTER TABLE candles ADD COLUMN {col} REAL")
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
        self.generation += 1
        self.rebuild_rag()
        out["deleted"] = deletable
        out["backup"] = str(backup)
        return out

    # ---- decisions ----
    def add_decision(self, **d: Any) -> int:
        d.setdefault("ts", time.time())
        d.setdefault("version", STRATEGY_VERSION)
        cols = ",".join(d)
        marks = ",".join("?" for _ in d)
        cur = self.db.execute(f"INSERT INTO decisions ({cols}) VALUES ({marks})", list(d.values()))
        self.db.commit()
        return int(cur.lastrowid or 0)

    def add_flip(self, ticker: str, stopped_side: str, lane: str, stake_usd: float, ts: float | None = None) -> bool:
        """Ask for a second look at the other side of a market a stop just sold. One request per market and side."""
        if self.db.execute("SELECT 1 FROM flips WHERE ticker=? AND stopped_side=?", (ticker, stopped_side)).fetchone():
            return False
        self.db.execute("INSERT INTO flips (ts, ticker, stopped_side, lane, stake_usd) VALUES (?,?,?,?,?)", (time.time() if ts is None else ts, ticker, stopped_side, lane, stake_usd))
        self.db.commit()
        return True

    def pending_flips(self) -> list[db.Row]:
        return self.db.execute("SELECT * FROM flips WHERE status='pending' ORDER BY ts").fetchall()

    def finish_flip(self, flip_id: int, status: str, note: str = "") -> None:
        self.db.execute("UPDATE flips SET status=?, note=? WHERE id=?", (status, note[:300], flip_id))
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
        """
        done = "(COALESCE(outcome,'') IN ('yes','no') OR exit_ts IS NOT NULL)"
        return self.db.execute(
            f"""SELECT id, ticker, lane, side, outcome, contracts, stake_usd, exit_price
                FROM decisions
                WHERE action='bet' AND stake_usd IS NOT NULL AND contracts > 0
                  AND (CASE WHEN mode = 'trade' THEN 1 ELSE 0 END) = ?
                  AND {'NOT ' + done if open_only else done}""",
            (1 if live else 0,),
        ).fetchall()

    def holds_sized_bet(self, ticker: str, lane: str | None = None) -> bool:
        """A sized bet on this market is already open (so a re-score must not stake it again).

        With `lane`, only that lane's bet counts: two lanes (an A/B pair, say) can each hold the same market.
        """
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

    def decided_since(self, ticker: str, since_ts: float) -> bool:
        row = self.db.execute(
            "SELECT 1 FROM decisions WHERE ticker=? AND ts>=? LIMIT 1", (ticker, since_ts)
        ).fetchone()
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
        self.generation += 1
        self.rag.index_settled([ticker])

    def settled_decisions(self, include_excluded: bool = False) -> list[db.Row]:
        """Settled decisions, minus excluded versions unless asked for everything."""
        extra, args = ("", []) if include_excluded else self._excluded_sql()
        return self.db.execute(
            f"SELECT * FROM decisions WHERE outcome IN ('yes','no'){extra} ORDER BY ts", args
        ).fetchall()

    def bet_tickers_in_event(self, event_ticker: str) -> set[str]:
        """Unresolved paper or real bets in this event. Empty when event_ticker is blank."""
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

    def rest_sell(self, ticker: str, env: str) -> db.Row | None:
        """The latest resting sell placed on this ticker, working or not."""
        return self.db.execute(
            "SELECT * FROM orders WHERE ticker=? AND env=? AND side='rest' ORDER BY id DESC LIMIT 1", (ticker, env)
        ).fetchone()

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

    def exited_within(self, ticker: str, lane: str, seconds: float, now: float | None = None) -> float:
        """Seconds still to wait if this lane sold this market less than `seconds` ago, else 0."""
        now = time.time() if now is None else now
        row = self.db.execute(
            """SELECT MAX(exit_ts) AS t FROM decisions
               WHERE ticker=? AND COALESCE(lane,'moonshot')=? AND action='bet' AND exit_ts IS NOT NULL""",
            (ticker, lane),
        ).fetchone()
        last = row["t"] if row and row["t"] is not None else None
        return max(0.0, last + seconds - now) if last is not None else 0.0

    def mark_exited(self, ticker: str, price: float, reason: str, lane: str | None = None, factors: str = "") -> None:
        self.db.execute(
            """UPDATE decisions SET exit_price=?, exit_ts=?, exit_reason=?, exit_factors=?
               WHERE ticker=? AND action='bet' AND outcome IS NULL AND exit_ts IS NULL
                 AND (? IS NULL OR COALESCE(lane,'moonshot')=?)""",
            (price, time.time(), reason[:300], factors or None, ticker, lane, lane),
        )
        self.db.commit()

    def open_bets(self) -> list[db.Row]:
        """Unresolved bets not yet stopped out. One row per ticker and lane (that lane's first bet)."""
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
