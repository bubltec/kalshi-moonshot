"""Retrieval over our own record: what happened the last times we rated a question like this.

One row per settled market (the first rating, the same rule `report` uses), in a full-text table
with the structured fields kept beside the text. Prices are never stored here, so a retrieved
case cannot hand the model the market's number.

Two things come back for a new question:
  cases      the most similar settled questions, same series first, never the question itself
  base rate  how often questions in the same series (else category, else lane) resolved yes

The class is small on purpose: a vector backend can replace `cases()` without touching callers.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .config import read_tuning

if TYPE_CHECKING:
    from .store import Store

SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS rag_cases USING fts5(
  title,
  body,
  ticker UNINDEXED,
  series UNINDEXED,
  category UNINDEXED,
  lane UNINDEXED,
  outcome UNINDEXED
);
"""

# Tokens that match almost every market and would pull unrelated settled cases.
STOP = frozenset({
    "will", "the", "over", "under", "more", "than", "before", "after", "wins", "game",
    "full", "points", "scored", "any", "this", "that", "with", "from", "have", "been",
    "was", "were", "and", "for", "not", "yes", "its", "who", "what", "when", "where",
    "how", "about", "into", "your", "their", "there", "which", "would", "could", "should",
    "then", "by", "of", "to", "in", "on", "at", "or", "a", "an", "be", "it", "is", "are",
})
MIN_BASE_RATE_N = 5


@dataclass(frozen=True)
class Case:
    title: str
    outcome: str
    body: str


@dataclass(frozen=True)
class BaseRate:
    scope: str  # "series KXGOLDD", "category Politics", "lane moonshot"
    yes: int
    n: int

    def line(self) -> str:
        return f"Base rate in our record: {self.yes} of {self.n} settled {self.scope} questions resolved yes."


def series_of(ticker: str) -> str:
    return ticker.split("-")[0] if ticker else ""


def query_for(text: str, limit: int = 8) -> str:
    words: list[str] = []
    for word in re.findall(r"[A-Za-z][A-Za-z0-9]{2,}", text.lower()):
        if word in STOP or word in words:
            continue
        words.append(word)
        if len(words) == limit:
            break
    return " OR ".join(words)


class Rag:
    def __init__(self, store: Store):
        self.store = store
        self.db = store.db
        self.db.executescript(SCHEMA)

    # ---- indexing ----
    def _skipped_lanes(self) -> set[str]:
        return {str(x) for x in (read_tuning(self.store.path.parent).get("rag_skip_lanes") or [])}

    def _row(self, r: sqlite3.Row) -> tuple:
        prior = f"prior estimate {r['model_p']:.2f}, resolved {r['outcome']}."
        note = (r["reason"] or "").replace(str(r["ask"]), "")
        return (
            r["title"] or "", f"{prior} {note}"[:400], r["ticker"], series_of(r["ticker"]),
            r["category"] or "", r["lane"] or "moonshot", r["outcome"],
        )

    def _eligible(self, rows: list[sqlite3.Row]) -> list[sqlite3.Row]:
        from .report import first_per_market  # local: report imports strategy

        skip = self._skipped_lanes()
        return [
            r for r in first_per_market(rows)
            if (r["lane"] or "moonshot") not in skip and r["lane"] != "commodity"
        ]

    def rebuild(self) -> int:
        """Drop and refill the whole index. Needed when excluded versions or skipped lanes change."""
        self.db.execute("DELETE FROM rag_cases")
        rows = self._eligible(self.store.settled_decisions())
        self.db.executemany(
            "INSERT INTO rag_cases (title, body, ticker, series, category, lane, outcome) "
            "VALUES (?,?,?,?,?,?,?)",
            [self._row(r) for r in rows],
        )
        self.db.commit()
        return len(rows)

    def index_settled(self, tickers: list[str]) -> None:
        """Add newly settled markets without touching the rest of the index."""
        if not tickers:
            return
        marks = ",".join("?" for _ in tickers)
        self.db.execute(f"DELETE FROM rag_cases WHERE ticker IN ({marks})", tickers)
        rows = [r for r in self.store.settled_decisions() if r["ticker"] in set(tickers)]
        self.db.executemany(
            "INSERT INTO rag_cases (title, body, ticker, series, category, lane, outcome) "
            "VALUES (?,?,?,?,?,?,?)",
            [self._row(r) for r in self._eligible(rows)],
        )
        self.db.commit()

    # ---- retrieval ----
    def cases(self, text: str, *, ticker: str = "", series: str = "", limit: int = 4) -> list[Case]:
        query = query_for(text)
        if not query:
            return []
        found: list[Case] = []
        seen: set[str] = set()
        scopes = ([("AND series = ?", [series])] if series else []) + [("", [])]
        for clause, args in scopes:
            try:
                rows = self.db.execute(
                    f"""SELECT title, outcome, body FROM rag_cases
                        WHERE rag_cases MATCH ? {clause} AND ticker != ?
                        ORDER BY bm25(rag_cases) LIMIT ?""",
                    [query, *args, ticker, limit],
                ).fetchall()
            except sqlite3.OperationalError:
                return found
            for r in rows:
                key = f"{r['title']}|{r['outcome']}"
                if key not in seen and len(found) < limit:
                    seen.add(key)
                    found.append(Case(r["title"], r["outcome"], r["body"]))
        return found

    def base_rate(self, *, series: str = "", category: str = "", lane: str = "") -> BaseRate | None:
        """Yes-rate of the narrowest group with enough settled cases."""
        for scope, col, val in (
            (f"series {series}", "series", series),
            (f"category {category}", "category", category),
            (f"lane {lane}", "lane", lane),
        ):
            if not val:
                continue
            row = self.db.execute(
                f"SELECT COUNT(*) AS n, SUM(outcome='yes') AS y FROM rag_cases WHERE {col}=?", (val,)
            ).fetchone()
            if row["n"] >= MIN_BASE_RATE_N:
                return BaseRate(scope, int(row["y"] or 0), int(row["n"]))
        return None

    def block(self, title: str, rules: str = "", *, ticker: str = "", series: str = "",
              category: str = "", lane: str = "", limit: int = 4) -> str:
        """Prompt text: similar settled cases, then the base rate when there is one."""
        series = series or series_of(ticker)
        lines = [
            f"- {c.title[:140]} -> {c.outcome}. {c.body[:180]}"
            for c in self.cases(f"{title} {rules}", ticker=ticker, series=series, limit=limit)
        ]
        rate = self.base_rate(series=series, category=category, lane=lane)
        if rate:
            lines.append(rate.line())
        return "\n".join(lines)
