"""SQLite: one file. Every decision is logged so the model can be scored later.

Settled decisions are also indexed in a local full-text table. The next estimate can
retrieve similar past cases. Prices are not stored in that index.
"""

from __future__ import annotations

import re
import shutil
import sqlite3
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

# Bump this whenever filters, prompt, or model logic change enough that old decisions
# should not be scored together with new ones. Every new decision is tagged with it.
#   v1  everything logged before versions existed (included live-number markets and sports totals)
#   v2  skips live-number markets (SKIP_NUMERIC)
STRATEGY_VERSION = "v2"
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
  version TEXT                   -- strategy version that produced this decision
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

CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,            -- scan | evaluate | resolve
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
  result TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_due ON jobs(status, run_at);

CREATE VIRTUAL TABLE IF NOT EXISTS rag_fts USING fts5(
  decision_id UNINDEXED,
  title,
  body,
  outcome
);
"""

# Tokens that match almost every market and would pull unrelated settled cases.
_RAG_STOP = frozenset({
    "will", "the", "over", "under", "more", "than", "before", "after", "wins", "game",
    "full", "points", "scored", "any", "this", "that", "with", "from", "have", "been",
    "was", "were", "and", "for", "not", "yes", "its", "who", "what", "when", "where",
    "how", "about", "into", "your", "their", "there", "which", "would", "could", "should",
    "then", "by", "of", "to", "in", "on", "at", "or", "a", "an", "be", "it", "is", "are",
})


class Store:
    def __init__(self, path: Path | str, exclude_versions: Iterable[str] = ()):
        self.path = Path(path)
        self.exclude_versions = frozenset(exclude_versions)
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self._migrate()
        self.rebuild_rag()

    def _migrate(self) -> None:
        """Add columns to a database created before they existed."""
        have = {r["name"] for r in self.db.execute("PRAGMA table_info(decisions)")}
        for name, ddl in (
            ("lane", "TEXT DEFAULT 'moonshot'"),
            ("context", "TEXT"),
            ("category", "TEXT"),
            ("version", "TEXT"),
        ):
            if name not in have:
                self.db.execute(f"ALTER TABLE decisions ADD COLUMN {name} {ddl}")
                if name == "version":
                    # Every row that exists before versions did was made by the old logic.
                    self.db.execute("UPDATE decisions SET version=?", (LEGACY_VERSION,))
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
        self.rebuild_rag()
        out["deleted"] = deletable
        out["backup"] = str(backup)  # type: ignore[assignment]
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
        self.rebuild_rag()

    def settled_decisions(self, include_excluded: bool = False) -> list[sqlite3.Row]:
        """Settled decisions, minus excluded versions unless asked for everything."""
        extra, args = ("", []) if include_excluded else self._excluded_sql()
        return self.db.execute(
            f"SELECT * FROM decisions WHERE outcome IN ('yes','no'){extra} ORDER BY ts", args
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

    # ---- local retrieval of settled decisions (no prices) ----
    def rebuild_rag(self) -> int:
        """Rebuild the search index from settled decisions that have a model probability."""
        self.db.execute("DELETE FROM rag_fts")
        n = 0
        for row in self.settled_decisions():
            if row["model_p"] is None:
                continue
            prior = f"prior estimate {row['model_p']:.2f}, resolved {row['outcome']}."
            note = (row["reason"] or "").replace(str(row["ask"]), "")
            self.db.execute(
                "INSERT INTO rag_fts (decision_id, title, body, outcome) VALUES (?,?,?,?)",
                (row["id"], row["title"] or "", f"{prior} {note}"[:400], row["outcome"]),
            )
            n += 1
        self.db.commit()
        return n

    def similar_text(self, title: str, rules: str = "", limit: int = 4) -> str:
        """Nearest settled cases for a new question. Empty when nothing useful matches."""
        query = _rag_query(f"{title} {rules}")
        if not query:
            return ""
        try:
            rows = self.db.execute(
                """SELECT title, outcome, body FROM rag_fts
                   WHERE rag_fts MATCH ? ORDER BY bm25(rag_fts) LIMIT ?""",
                (query, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            return ""
        lines = []
        seen: set[str] = set()
        for row in rows:
            key = f"{row['title']}|{row['outcome']}"
            if key in seen:
                continue
            seen.add(key)
            lines.append(f"- {row['title'][:140]} -> {row['outcome']}. {row['body'][:180]}")
        return "\n".join(lines)


def _rag_query(text: str) -> str:
    words = []
    for word in re.findall(r"[A-Za-z][A-Za-z0-9]{2,}", text.lower()):
        if word in _RAG_STOP or word in words:
            continue
        words.append(word)
        if len(words) == 8:
            break
    return " OR ".join(words)
