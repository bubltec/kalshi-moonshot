"""SQLite: one file, three tables. Every decision is logged (traded or not) so the model's
probabilities can later be scored against what actually happened."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

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
  settled_ts REAL
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
  order_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_orders_ts ON orders(ts);
"""


class Store:
    def __init__(self, path: Path | str):
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    # ---- decisions ----
    def add_decision(self, **d: Any) -> int:
        d.setdefault("ts", time.time())
        cols = ",".join(d)
        marks = ",".join("?" for _ in d)
        cur = self.db.execute(f"INSERT INTO decisions ({cols}) VALUES ({marks})", list(d.values()))
        self.db.commit()
        return int(cur.lastrowid or 0)

    def unresolved_tickers(self) -> list[str]:
        rows = self.db.execute(
            "SELECT DISTINCT ticker FROM decisions WHERE outcome IS NULL"
        ).fetchall()
        return [r["ticker"] for r in rows]

    def set_outcome(self, ticker: str, outcome: str) -> None:
        self.db.execute(
            "UPDATE decisions SET outcome=?, settled_ts=? WHERE ticker=? AND outcome IS NULL",
            (outcome, time.time(), ticker),
        )
        self.db.commit()

    def settled_decisions(self) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM decisions WHERE outcome IN ('yes','no') ORDER BY ts"
        ).fetchall()

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

    def spent_since(self, since_ts: float, env: str) -> float:
        row = self.db.execute(
            "SELECT COALESCE(SUM(cost_usd),0) AS s FROM orders WHERE ts>=? AND env=?",
            (since_ts, env),
        ).fetchone()
        return float(row["s"])

    def open_position_tickers(self, env: str) -> set[str]:
        """Tickers we hold: filled buys whose market has not settled yet."""
        rows = self.db.execute(
            """SELECT DISTINCT o.ticker FROM orders o
               WHERE o.env=? AND o.fill_count>0
                 AND NOT EXISTS (SELECT 1 FROM decisions d
                                 WHERE d.ticker=o.ticker AND d.outcome IN ('yes','no'))""",
            (env,),
        ).fetchall()
        return {r["ticker"] for r in rows}
