"""SQLite: one file. Every decision is logged so the model can be scored later.

Settled decisions are also indexed in a local full-text table. The next estimate can
retrieve similar past cases. Prices are not stored in that index.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

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
  contracts INTEGER,             -- paper or real size sized by sizing.py (first bet per market only)
  stake_usd REAL                 -- cost incl. fee of those contracts
);
CREATE INDEX IF NOT EXISTS idx_decisions_ticker ON decisions(ticker);

CREATE TABLE IF NOT EXISTS orders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  ticker TEXT NOT NULL,
  env TEXT NOT NULL,
  count INTEGER NOT NULL,
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
CREATE INDEX IF NOT EXISTS idx_jobs_due ON jobs(status, run_at);
"""

class Store:
    def __init__(self, path: Path | str, exclude_versions: Iterable[str] = ()):
        self.path = Path(path)
        self.exclude_versions = frozenset(exclude_versions)
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.execute("DROP TABLE IF EXISTS rag_fts")  # replaced by rag_cases
        self._migrate()
        self.generation = 0  # bumped whenever settled data changes; caches key on it
        self.rag = Rag(self)
        self.calibrator = Calibrator(self)
        from .bankroll import Bankroll  # local: bankroll imports this module

        self.bankroll = Bankroll(self)
        self.rag.rebuild()

    def _migrate(self) -> None:
        """Add columns to a database created before they existed."""
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
            ("contracts", "INTEGER"),
            ("stake_usd", "REAL"),
        ):
            if name not in have:
                self.db.execute(f"ALTER TABLE decisions ADD COLUMN {name} {ddl}")
                if name == "version":
                    # Every row that exists before versions did was made by the old logic.
                    self.db.execute("UPDATE decisions SET version=?", (LEGACY_VERSION,))
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

    def _excluded_sql(self) -> tuple[str, list[str]]:
        if not self.exclude_versions:
            return "", []
        marks = ",".join("?" for _ in self.exclude_versions)
        return f" AND COALESCE(version,'{LEGACY_VERSION}') NOT IN ({marks})", sorted(self.exclude_versions)

    def version_counts(self) -> list[sqlite3.Row]:
        """Per version: decisions logged and how many of those have settled."""
        return self.db.execute(
            f"""SELECT COALESCE(version,'{LEGACY_VERSION}') AS version, COUNT(*) AS decisions,
                       SUM(outcome IN ('yes','no')) AS settled
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
        backup = self.path.with_name(f"{self.path.name}.bak-{int(time.time())}")
        shutil.copy2(self.path, backup)
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
        backup = self.path.with_name(f"{self.path.name}.bak-{int(time.time())}")
        shutil.copy2(self.path, backup)
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

    def paper_bets(self, open_only: bool, live: bool = False) -> list[sqlite3.Row]:
        """Sized bets (one per market, the first). open_only: still riding; else: finished.

        live=False: paper bets, those made while their lane was not placing orders.
        live=True: bets that were sent as real orders.
        """
        done = "(COALESCE(outcome,'') IN ('yes','no') OR exit_ts IS NOT NULL)"
        return self.db.execute(
            f"""SELECT id, ticker, lane, side, outcome, contracts, stake_usd, exit_price
                FROM decisions
                WHERE action='bet' AND stake_usd IS NOT NULL AND contracts > 0
                  AND (mode = 'trade') = ?
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

    def shadow_rows(self, settled_only: bool = True) -> list[sqlite3.Row]:
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

    def settled_decisions(self, include_excluded: bool = False) -> list[sqlite3.Row]:
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

    def recent_decisions(self, limit: int = 20) -> list[sqlite3.Row]:
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

    def mark_exited(self, ticker: str, price: float, reason: str, lane: str | None = None) -> None:
        self.db.execute(
            """UPDATE decisions SET exit_price=?, exit_ts=?, exit_reason=?
               WHERE ticker=? AND action='bet' AND outcome IS NULL AND exit_ts IS NULL
                 AND (? IS NULL OR COALESCE(lane,'moonshot')=?)""",
            (price, time.time(), reason[:300], ticker, lane, lane),
        )
        self.db.commit()

    def open_bets(self) -> list[sqlite3.Row]:
        """Unresolved bets not yet stopped out. One row per ticker and lane (that lane's first bet)."""
        return self.db.execute(
            """SELECT d.* FROM decisions d
               JOIN (
                 SELECT ticker, COALESCE(lane,'moonshot') AS lane, MIN(id) AS id FROM decisions
                 WHERE action='bet' AND outcome IS NULL AND exit_ts IS NULL
                 GROUP BY ticker, COALESCE(lane,'moonshot')
               ) first ON d.id = first.id
               ORDER BY d.ts"""
        ).fetchall()

    def open_positions(self, env: str) -> list[sqlite3.Row]:
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
                 HAVING contracts > 0
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
            f"- {c.title[:140]} -> {c.outcome}. {c.body[:180]}"
            for c in self.rag.cases(f"{title} {rules}", limit=limit)
        )
