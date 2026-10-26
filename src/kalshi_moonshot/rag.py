"""Retrieval over our own record: what happened the last times we rated a question like this.

One row per settled market (the first rating, the same rule `report` uses), in a full-text table
with the structured fields kept beside the text. Prices are never stored here, so a retrieved
case cannot hand the model the market's number.

Two things come back for a new question:
  cases      the most similar settled questions, same series first, never the question itself
  base rate  how often questions in the same series (else category, else lane) resolved yes

Every case carries when it settled, and that is not bookkeeping: markets mutate, so a case from
last week is evidence about this market and a case from last season is evidence about a market
that may no longer exist. Retrieved cases say their age, and a base rate is read from the last
RECENT_DAYS when that window holds enough cases, so the record the model leans on is the recent
one rather than an all-time average over regimes.

The class is small on purpose: a vector backend can replace `cases()` without touching callers.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from . import db
from .config import read_tuning

if TYPE_CHECKING:
    from .store import Store

SCHEMA_PG = """
CREATE TABLE IF NOT EXISTS rag_cases (
  title TEXT, body TEXT, ticker TEXT, series TEXT, category TEXT, lane TEXT, outcome TEXT, settled_ts REAL,
  tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', coalesce(title, '') || ' ' || coalesce(body, ''))) STORED
);
CREATE INDEX IF NOT EXISTS idx_rag_tsv ON rag_cases USING GIN (tsv);
CREATE INDEX IF NOT EXISTS idx_rag_series ON rag_cases(series);
"""

SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS rag_cases USING fts5(
  title,
  body,
  ticker UNINDEXED,
  series UNINDEXED,
  category UNINDEXED,
  lane UNINDEXED,
  outcome UNINDEXED,
  settled_ts UNINDEXED
);
"""

RECENT_DAYS = 30  # a base rate is read from cases settled within this window when it holds enough of them (see base_rate)

# Tokens that match almost every market and would pull unrelated settled cases.
STOP = frozenset({
    "will", "the", "over", "under", "more", "than", "before", "after", "wins", "game",
    "full", "points", "scored", "any", "this", "that", "with", "from", "have", "been",
    "was", "were", "and", "for", "not", "yes", "its", "who", "what", "when", "where",
    "how", "about", "into", "your", "their", "there", "which", "would", "could", "should",
    "then", "by", "of", "to", "in", "on", "at", "or", "a", "an", "be", "it", "is", "are",
})
MIN_BASE_RATE_N = 5


def age_of(settled_ts: float, now: float | None = None) -> str:
    """How long ago a case settled, in the units a reader thinks in: "today", "3d ago", "5w ago", "2mo ago"."""
    days = max(0.0, ((now or time.time()) - settled_ts) / 86400.0)
    if days < 1:
        return "today"
    if days < 14:
        return f"{int(days)}d ago"
    if days < 60:
        return f"{int(days / 7)}w ago"
    return f"{int(days / 30)}mo ago"


@dataclass(frozen=True)
class Case:
    title: str
    outcome: str
    body: str
    settled_ts: float = 0.0

    def line(self) -> str:
        age = f" (settled {age_of(self.settled_ts)})" if self.settled_ts else ""
        return f"- {self.title[:140]} -> {self.outcome}{age}. {self.body[:180]}"


@dataclass(frozen=True)
class BaseRate:
    scope: str  # "series KXGOLDD", "category Politics", "lane moonshot"
    yes: int
    n: int
    recent: bool = False  # read from the last RECENT_DAYS only, not the whole record

    def line(self) -> str:
        window = f" (last {RECENT_DAYS} days)" if self.recent else ""
        return f"Base rate in our record: {self.yes} of {self.n} settled {self.scope} questions resolved yes{window}."


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
    def __init__(self, store: Store, create: bool = True):
        self.store = store
        self.db = store.db
        self.pg = db.is_pg(self.db)
        if create:
            self._create()

    def _create(self) -> None:
        """Make the index, adding `settled_ts` to one made before cases carried it, and refill if that left times missing."""
        if self.pg:
            self.db.executescript(SCHEMA_PG)
            self.db.execute("ALTER TABLE rag_cases ADD COLUMN IF NOT EXISTS settled_ts REAL")
        else:
            cols = {r["name"] for r in self.db.execute("PRAGMA table_info(rag_cases)")}
            if cols and "settled_ts" not in cols:
                self.db.execute("DROP TABLE rag_cases")  # FTS5 cannot grow a column in place; refilled just below
            self.db.executescript(SCHEMA)
        stale = self.db.execute("SELECT COUNT(*) AS n FROM rag_cases WHERE settled_ts IS NULL").fetchone()["n"]
        if stale:
            self.rebuild()  # settlement times come from the decisions themselves, so this backfills exactly

    # ---- indexing ----
    def _skipped_lanes(self) -> set[str]:
        return {str(x) for x in (read_tuning(self.store.path.parent).get("rag_skip_lanes") or [])}

    def _row(self, r: db.Row) -> tuple:
        prior = f"prior estimate {r['model_p']:.2f}, resolved {r['outcome']}."
        note = (r["reason"] or "").replace(str(r["ask"]), "")
        return (
            r["title"] or "", f"{prior} {note}"[:400], r["ticker"], series_of(r["ticker"]),
            r["category"] or "", r["lane"] or "moonshot", r["outcome"],
            float(r["settled_ts"] or r["ts"] or 0.0),
        )

    def _eligible(self, rows: list[db.Row]) -> list[db.Row]:
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
            "INSERT INTO rag_cases (title, body, ticker, series, category, lane, outcome, settled_ts) "
            "VALUES (?,?,?,?,?,?,?,?)",
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
            "INSERT INTO rag_cases (title, body, ticker, series, category, lane, outcome, settled_ts) "
            "VALUES (?,?,?,?,?,?,?,?)",
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
                if self.pg:
                    tsq = query.replace(" OR ", " | ")
                    rows = self.db.execute(
                        f"""SELECT title, outcome, body, settled_ts FROM rag_cases
                            WHERE tsv @@ to_tsquery('english', ?) {clause} AND ticker != ?
                            ORDER BY ts_rank(tsv, to_tsquery('english', ?)) DESC LIMIT ?""",
                        [tsq, *args, ticker, tsq, limit],
                    ).fetchall()
                else:
                    rows = self.db.execute(
                        f"""SELECT title, outcome, body, settled_ts FROM rag_cases
                            WHERE rag_cases MATCH ? {clause} AND ticker != ?
                            ORDER BY bm25(rag_cases) LIMIT ?""",
                        [query, *args, ticker, limit],
                    ).fetchall()
            except Exception:  # a query the engine cannot parse is no match, not a crash
                return found
            for r in rows:
                key = f"{r['title']}|{r['outcome']}"
                if key not in seen and len(found) < limit:
                    seen.add(key)
                    found.append(Case(r["title"], r["outcome"], r["body"], float(r["settled_ts"] or 0.0)))
        return found

    def base_rate(self, *, series: str = "", category: str = "", lane: str = "") -> BaseRate | None:
        """Yes-rate of the narrowest group with enough settled cases.

        Within a group the last RECENT_DAYS speak first when they hold enough cases: the market
        mutates, and an all-time rate averages regimes that no longer trade. Only when the recent
        window is too thin does the whole record answer.
        """
        since = time.time() - RECENT_DAYS * 86400.0
        for scope, col, val in (
            (f"series {series}", "series", series),
            (f"category {category}", "category", category),
            (f"lane {lane}", "lane", lane),
        ):
            if not val:
                continue
            for window, args in ((" AND settled_ts>=?", (val, since)), ("", (val,))):
                row = self.db.execute(
                    f"SELECT COUNT(*) AS n, SUM(CASE WHEN outcome='yes' THEN 1 ELSE 0 END) AS y FROM rag_cases WHERE {col}=?{window}", args
                ).fetchone()
                if row["n"] >= MIN_BASE_RATE_N:
                    return BaseRate(scope, int(row["y"] or 0), int(row["n"]), recent=bool(window))
        return None

    def block(self, title: str, rules: str = "", *, ticker: str = "", series: str = "",
              category: str = "", lane: str = "", limit: int = 4) -> str:
        """Prompt text: similar settled cases, then the base rate when there is one."""
        series = series or series_of(ticker)
        lines = [
            c.line()
            for c in self.cases(f"{title} {rules}", ticker=ticker, series=series, limit=limit)
        ]
        rate = self.base_rate(series=series, category=category, lane=lane)
        if rate:
            lines.append(rate.line())
        return "\n".join(lines)
