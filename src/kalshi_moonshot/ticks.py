"""Every settlement-index tick of the run, in memory, so a decision never has to ask the database what the spot has been doing.

One pair of float arrays per index (times, values): 16 bytes a tick, so a day of a 1 Hz feed is about 1.4 MB. tape.Recent.note_index feeds it as ticks arrive; warm() fills the hours before this
process started, once, from the tape. A read is two bisects and a slice. Nothing here touches the database wrapper: warm() uses a plain read on the tape, and only at start-up.
"""

from __future__ import annotations

import bisect
import itertools
import logging
import threading
import time
from array import array
from collections import deque
from collections.abc import Sequence
from typing import Any

log = logging.getLogger("kalshi_moonshot.ticks")

KEEP_SECONDS = 12 * 3600.0  # how much of each index is kept; older ticks are dropped in chunks
KEEP_BY_PREFIX = {"hl:": 3600.0}  # 2026-11-08: Hyperliquid perp trades (futflow.py) arrive many times a second, so a symbol with this prefix keeps an hour, enough for a rolling typical minute and a spike read
TRIM_SLACK = 1800.0  # trim only when this much extra has piled up, so a trim is rare and cheap per tick


class TickHistory:
    def __init__(self, keep: float = KEEP_SECONDS) -> None:
        self.keep = keep
        self.slack = min(TRIM_SLACK, keep)
        self.keep_by_prefix = dict(KEEP_BY_PREFIX)
        self._lock = threading.Lock()
        self._ts: dict[str, array] = {}
        self._val: dict[str, array] = {}
        self._from: dict[str, float] = {}  # the earliest time this index is known to be complete from (a warm-up start, or the first live tick)

    def note(self, index_id: str, ts: float, value: float) -> None:
        if value <= 0:
            return
        with self._lock:
            t = self._ts.get(index_id)
            if t is None:
                t = self._ts[index_id] = array("d")
                self._val[index_id] = array("d")
                self._from[index_id] = ts
            elif t and ts < t[-1]:
                return  # a late or duplicate tick: the series stays ordered
            t.append(ts)
            self._val[index_id].append(value)
            keep = next((k for p, k in self.keep_by_prefix.items() if index_id.startswith(p)), self.keep)
            if ts - t[0] > keep + min(self.slack, keep):
                cut = bisect.bisect_left(t, ts - keep)
                del t[:cut]
                del self._val[index_id][:cut]
                self._from[index_id] = max(self._from[index_id], t[0])

    def warm(self, index_id: str, rows: Sequence[tuple[float, float]], loaded_from: float) -> None:
        """Put older ticks (ascending, from the tape) in front of whatever has arrived live. Done once per index."""
        with self._lock:
            if index_id in self._from and self._from[index_id] <= loaded_from:
                return
            live_ts, live_val = self._ts.get(index_id), self._val.get(index_id)
            t, v = array("d"), array("d")
            first_live = live_ts[0] if live_ts else float("inf")
            for ts, value in rows:
                if ts < first_live and value > 0:
                    t.append(ts)
                    v.append(value)
            if live_ts is not None and live_val is not None:
                t.extend(live_ts)
                v.extend(live_val)
            self._ts[index_id], self._val[index_id] = t, v
            self._from[index_id] = loaded_from

    def covers(self, index_id: str, start: float) -> bool:
        with self._lock:
            return index_id in self._from and self._from[index_id] <= start and bool(self._ts.get(index_id))

    def window(self, index_id: str, start: float, end: float) -> tuple[list[float], list[float]] | None:
        """(times, values) with start <= ts <= end, or None when the history does not reach `start` (the caller then asks the tape)."""
        with self._lock:
            if index_id not in self._from or self._from[index_id] > start or not self._ts.get(index_id):
                return None
            t, v = self._ts[index_id], self._val[index_id]
            lo, hi = bisect.bisect_left(t, start), bisect.bisect_right(t, end)
            return t[lo:hi].tolist(), v[lo:hi].tolist()

    def last_at_or_before(self, index_id: str, ts: float) -> tuple[float, float] | None:
        """The last tick (time, value) at or before `ts`, however old (a gridded path carries it forward across a pause in the feed, such as a commodity's daily session break), or None."""
        with self._lock:
            t = self._ts.get(index_id)
            if not t:
                return None
            i = bisect.bisect_right(t, ts) - 1
            return (t[i], self._val[index_id][i]) if i >= 0 else None

    def first_at_or_after(self, index_id: str, start: float, within: float) -> float | None:
        """The first tick value at or after `start` and no more than `within` seconds later (a window-open reference), or None."""
        with self._lock:
            if index_id not in self._from or self._from[index_id] > start or not self._ts.get(index_id):
                return None
            t = self._ts[index_id]
            i = bisect.bisect_left(t, start)
            if i >= len(t) or t[i] - start > within:
                return None
            return self._val[index_id][i]

    def size(self, index_id: str) -> int:
        with self._lock:
            return len(self._ts.get(index_id, ()))

    def clear(self) -> None:
        with self._lock:
            self._ts.clear()
            self._val.clear()
            self._from.clear()


history = TickHistory()


class MidHistory:
    """The last hour of every market's quotes (yes side), in memory (2026-11-11, user: "every market ticket should have the past 15 minutes, if we could store the past hour even better ... in memory"), so a decision asks the market's own
    path without a query. One row a second per ticker (the latest quote of the second): (time, mid, bid, ask). Fed by tape.Recent.note_quote; filled from the tape at start (warm_mids_from_tape) so a restart does not begin every
    open market with an empty path; a ticker whose last quote is older than `keep` is dropped, so closed windows do not pile up (before this the dict kept every ticker since the start). series_rows stitches one series' windows into the hour's path."""

    def __init__(self, keep: float = 3600.0) -> None:
        self.keep = keep
        self._lock = threading.Lock()
        self._rows: dict[str, deque[tuple[float, float, float, float]]] = {}
        self._n = 0

    def note(self, ticker: str, ts: float, bid: float, ask: float) -> None:
        if bid <= 0 or ask <= 0:
            return
        with self._lock:
            rows = self._rows.get(ticker)
            if rows is None:
                rows = self._rows[ticker] = deque()
            row = (ts, (bid + ask) / 2.0, bid, ask)
            if rows and int(rows[-1][0]) == int(ts):
                rows[-1] = row  # one row a second: the latest quote of that second
            else:
                rows.append(row)
            cut = ts - self.keep
            while rows and rows[0][0] < cut:
                rows.popleft()
            self._n += 1
            if self._n % 2000 == 0:
                self._prune_locked(ts)

    def _prune_locked(self, now: float) -> None:
        for tk in [tk for tk, r in self._rows.items() if not r or r[-1][0] < now - self.keep]:
            del self._rows[tk]

    def prune(self, now: float) -> int:
        """Drop the tickers whose last quote is older than `keep`. Returns how many went."""
        with self._lock:
            before = len(self._rows)
            self._prune_locked(now)
            return before - len(self._rows)

    def warm(self, rows_by_ticker: dict[str, Sequence[tuple[float, float, float]]]) -> int:
        """Put older (time, bid, ask) quotes in front of what a ticker already holds (a restart's tape load). A ticker keeps its live rows; only quotes older than its first live row are added, on the one-row-a-second grid."""
        added = 0
        with self._lock:
            for tk, quotes in rows_by_ticker.items():
                have = self._rows.get(tk)
                first = have[0][0] if have else float("inf")
                old: list[tuple[float, float, float, float]] = []
                for ts, bid, ask in quotes:
                    if ts >= first or bid <= 0 or ask <= 0:
                        continue
                    row = (float(ts), (bid + ask) / 2.0, float(bid), float(ask))
                    if old and int(old[-1][0]) == int(ts):
                        old[-1] = row
                    else:
                        old.append(row)
                if old:
                    if have is None:
                        have = self._rows[tk] = deque()
                    have.extendleft(reversed(old))
                    added += len(old)
        return added

    def quotes(self, ticker: str) -> list[tuple[float, float, float]]:
        """A copy of the market's (time, YES bid, YES ask) rows held in memory, oldest first."""
        with self._lock:
            return [(r[0], r[2], r[3]) for r in self._rows.get(ticker, ())]

    def series_rows(self, series: str, now: float, seconds: float = 3600.0) -> list[tuple[float, float]]:
        """The (time, YES mid) path of a whole series over the last `seconds`: its windows' tickers stitched in time order, so the hour before a window opens is one list."""
        start = now - seconds
        out: list[tuple[float, float]] = []
        with self._lock:
            for tk, rows in self._rows.items():
                if tk.startswith(series + "-"):
                    out += [(r[0], r[1]) for r in rows if start <= r[0] <= now]
        out.sort()
        return out

    def crossings(self, ticker: str, now: float, seconds: float = 300.0, level: float = 0.5, step: float = 5.0) -> int | None:
        """How many times the mid crossed `level` in the last `seconds`, on a `step` grid that carries the last quote forward (the way the study counted it). None when the history is too thin to say:
        under 40 of the 60 grid points have a quote within 20 s, or the first quote is later than the start of the span."""
        with self._lock:
            rows = [(r[0], r[1]) for r in self._rows.get(ticker, ())]
        if not rows:
            return None
        times = [r[0] for r in rows]
        if times[0] > now - seconds + 20.0:
            return None
        grid = []
        n = int(seconds / step)
        for i in range(n + 1):
            t = now - seconds + i * step
            k = bisect.bisect_right(times, t) - 1
            if k >= 0 and t - times[k] <= 20.0:
                grid.append(rows[k][1])
        if len(grid) < int(n * 2 / 3):
            return None
        return sum(1 for a, b in itertools.pairwise(grid) if (a - level) * (b - level) < 0)

    def rows(self, ticker: str) -> list[tuple[float, float]]:
        """A copy of the market's (time, YES mid) rows held in memory, oldest first."""
        with self._lock:
            return [(r[0], r[1]) for r in self._rows.get(ticker, ())]

    def clear(self) -> None:
        with self._lock:
            self._rows.clear()


mids = MidHistory()


class Strikes:
    """The strike (floor_strike) of every open market the tape has listed, in memory. A live market's strike is not in the windows table until it settles, so a buy that needs the strike (a crypto variety, the cushion)
    reads it here. Filled by tape.refresh from the 15-minute listings every 20 s; the oldest entries are dropped past `keep`."""

    def __init__(self, keep: int = 1500) -> None:
        self.keep = keep
        self._lock = threading.Lock()
        self._v: dict[str, float] = {}

    def note(self, ticker: str, strike: float) -> None:
        if strike <= 0:
            return
        with self._lock:
            self._v.pop(ticker, None)
            self._v[ticker] = strike
            while len(self._v) > self.keep:
                self._v.pop(next(iter(self._v)))

    def get(self, ticker: str) -> float | None:
        with self._lock:
            return self._v.get(ticker)

    def clear(self) -> None:
        with self._lock:
            self._v.clear()


strikes = Strikes()


def warm_from_tape(s, indexes: Sequence[str], now: float, hours: float = 8.0, until: float | None = None) -> int:
    """Load the last `hours` of each index from the tape into `history` (8 by default: the volatility regime reads the last 6 hours and 2 minutes, so a 6-hour load would never cover it). Meant for a background thread at start-up; returns how many ticks were loaded."""
    from . import db

    if not db.tape_exists(s.tape_path):
        return 0
    start = now - hours * 3600.0
    end = now if until is None else until  # the second stage of the reload loads only what is older than the first
    tape = db.reader(s.tape_path)
    total = 0
    for index_id in indexes:
        try:
            rows = [(float(ts), float(v)) for ts, v in tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND ts>? AND ts<=? AND value>0 ORDER BY ts", (index_id, start, end))]
        except Exception:
            log.warning("ticks: could not warm %s", index_id, exc_info=True)
            continue
        history.warm(index_id, rows, start)
        total += len(rows)
    return total


def recent_15m_tickers(s, now: float, hours: float = 1.0) -> list[str]:
    """The tickers of every 15-minute market that was open in the last `hours` (and the one about to open), built from the series and the quarter-hours: SERIES-YYMONDDHHMM-MM with the close time in New York, as KXBTC15M-26NOV141830-30. A lookup of these by name uses the tape's (ticker, ts) index and touches a few hundred pages; the old filter, ticker LIKE '%15M-%', has a leading wildcard, read the whole index (about 264,000 buffers, 79,000 of them from disk, for 31,000 rows) and was most of the reload's wait after a restart."""
    from datetime import UTC, datetime
    from zoneinfo import ZoneInfo

    from .commodities import load_instruments

    et = ZoneInfo("America/New_York")
    first = int((now - hours * 3600.0) // 900.0) * 900
    series = [f"{i.prefix}15M" for i in load_instruments(s.data_dir) if "15M" in i.suffixes]
    out: list[str] = []
    for close in range(first, int(now) + 2 * 900, 900):
        t = datetime.fromtimestamp(close, UTC).astimezone(et)
        stamp = f"{t:%y}{t:%b}{t:%d}{t:%H%M}".upper()
        out += [f"{ser}-{stamp}-{t:%M}" for ser in series]
    return out


def _quote_rows(s, now: float, hours: float) -> list[tuple[str, float, float, float]]:
    """(ticker, ts, bid, ask) of every quote in the last `hours` of the 15-minute markets, oldest first, by ticker name (recent_15m_tickers). One-sided quotes are in it (a decided window's dead side has no bid): the report's hole is read from all of them, and the memory loads only those with both sides."""
    from . import db

    if not db.tape_exists(s.tape_path):
        return []
    tickers = recent_15m_tickers(s, now, hours)
    tape = db.reader(s.tape_path)
    rows: list[tuple[str, float, float, float]] = []
    try:
        for k in range(0, len(tickers), 100):
            chunk = tickers[k : k + 100]
            marks = ",".join("?" * len(chunk))
            got = tape.execute(f"SELECT ticker, ts, bid, ask FROM quotes WHERE ticker IN ({marks}) AND ts>? AND ts<=?", (*chunk, now - hours * 3600.0, now)).fetchall()
            rows += [(str(r[0]), float(r[1]), float(r[2] or 0.0), float(r[3] or 0.0)) for r in got]
    except Exception:
        log.warning("ticks: could not read the market quotes", exc_info=True)
        return []
    rows.sort(key=lambda r: r[1])
    return rows


def warm_mids_from_tape(s, now: float, hours: float = 1.0) -> int:
    """Load the last `hours` of every 15-minute market's quotes from the tape into `mids`, so a restart does not leave the open windows with only what arrived since. Meant for a bot that has just started."""
    by: dict[str, list[tuple[float, float, float]]] = {}
    for tk, ts, bid, ask in _quote_rows(s, now, hours):
        if bid > 0 and ask > 0:
            by.setdefault(tk, []).append((ts, bid, ask))
    return mids.warm(by)


# ---- the reload of memory at start-up (2026-11-14, user: "I just want to make sure that's filled when we rebuild or restart") ----
RELOAD_LIMIT = 180.0  # buys are held for the reload this long at most: a reload that hangs must not stop the bot trading
HOLE_MIN = 10.0  # seconds with no quote from ANY 15-minute market before it counts as a hole in the record (one quiet market is not a hole; all of them silent is the feed being down)
_reload_began = 0.0
_reload_done = threading.Event()
reload_report: dict[str, float] = {}


def reload_begin(now: float | None = None) -> None:
    """Mark the reload as started, so buys wait for it (reload_pending). Called before the reload thread starts, so there is no moment when the bot is up, memory is empty and nothing says so."""
    global _reload_began
    _reload_done.clear()
    _reload_began = time.time() if now is None else now


def reload_pending(now: float | None = None) -> bool:
    """True from reload_begin until the reload has finished, or RELOAD_LIMIT seconds have passed. False when no reload was ever started (a test, a script, a process with no tape)."""
    if _reload_began <= 0 or _reload_done.is_set():
        return False
    return (time.time() if now is None else now) - _reload_began < RELOAD_LIMIT


def longest_hole(times: Sequence[float], start: float, end: float) -> tuple[float, float]:
    """The longest stretch with no reading between `start` and `end`, given the sorted times of every reading in that span: (its length in seconds, the time it ended). The stretch from the last reading to `end` counts (after a restart that is the time the recorder was down)."""
    worst, at, prev = 0.0, start, start
    for t in itertools.chain(times, (end,)):
        if t < start:
            continue
        if t - prev > worst:
            worst, at = t - prev, t
        prev = max(prev, t)
    return worst, at


FIRST_STAGE_HOURS = 1.4  # the first part of the reload: what the decision path reads out of memory (the hour's width and view, the hour of falls, the quote history), a little over an hour and a quarter. Buys wait for this and no more


def reload_from_tape(s, indexes: Sequence[str], now: float | None = None) -> dict[str, float]:
    """Fill memory from the tape at start-up, in two stages, and say how complete it is.
    Stage 1 (FIRST_STAGE_HOURS of every index, and the last hour of every 15-minute market's quotes) is what the decisions read; buys are released the moment it is in memory. Stage 2, behind it and without holding anything, adds the rest of the 8 hours of index ticks for the readers that look further back (the volatility regime, the daily profile); a reader that wants more than is there yet asks the tape, as it did before the reload existed (ticks.history.window returns None).
    Until 2026-11-14 it was one load of 8 hours (about 700,000 ticks) with everything waiting on it: 74 s and 104 s at the 16:16 and 18:31 restarts. Reports the rows loaded, the seconds each stage took, and the longest hole in the last hour of quotes (a stretch of HOLE_MIN seconds or more in which no 15-minute market quoted: the recorder was down). Never raises: a failed reload still lets the bot trade."""

    began = time.time()
    now = began if now is None else now
    report: dict[str, float] = {"ticks": 0, "quotes": 0, "markets": 0, "hole": 0.0, "hole_end": 0.0, "seconds": 0.0, "seconds_all": 0.0}
    try:
        report["ticks"] = warm_from_tape(s, indexes, now, hours=FIRST_STAGE_HOURS)
        rows = _quote_rows(s, now, 1.0)
        by: dict[str, list[tuple[float, float, float]]] = {}
        for tk, ts, bid, ask in rows:
            if bid > 0 and ask > 0:
                by.setdefault(tk, []).append((ts, bid, ask))
        report["quotes"] = mids.warm(by)
        report["markets"] = len(mids._rows)
        times = [r[1] for r in rows]  # the rows just read, one-sided quotes included (a decided window's dead side: leaving them out read a decisive close as a 50 s hole at the 16:15 restart): the hole is read from them, with no second query
        if times:
            report["hole"], report["hole_end"] = longest_hole(times, max(times[0], now - 3600.0), now)
    except Exception:
        log.warning("ticks: the reload of memory from the tape failed; the bot trades on what arrives from here", exc_info=True)
    finally:
        report["seconds"] = time.time() - began
        reload_report.clear()
        reload_report.update(report)
        _reload_done.set()  # the buys are released here: the rest is for readers that can wait
    hole = report["hole"]
    log.log(logging.WARNING if hole >= HOLE_MIN else logging.INFO,
            "memory reloaded from the tape in %.1fs (buys released): %d index ticks (the last %.1f h of %d indexes), %d quotes of the last hour (%d markets); %s",
            report["seconds"], report["ticks"], FIRST_STAGE_HOURS, len(indexes), report["quotes"], report["markets"],
            f"the longest hole in the last hour of quotes is {hole:.0f}s, ending {time.strftime('%H:%M:%S', time.gmtime(report['hole_end']))} UTC (not filled: no quotes were recorded then)" if hole >= HOLE_MIN else "no hole in the last hour of quotes")
    try:
        more = warm_from_tape(s, indexes, now, hours=8.0, until=now - FIRST_STAGE_HOURS * 3600.0)  # stage 2: only the ticks older than stage 1 go in front of what is there
        report["ticks"] += more
        report["seconds_all"] = time.time() - began
        log.info("memory: the rest of the 8 hours of index ticks loaded behind the decisions: %d more ticks, %.1fs from the start", more, report["seconds_all"])
    except Exception:
        log.warning("ticks: the second stage of the reload failed; readers that look back further ask the tape", exc_info=True)
    return report


class BookStore:
    """Each market's order book, kept from Kalshi's orderbook_delta websocket channel (2026-11-16, user: "it has websocket events; don't use the rest calls for the order book, or queue"): a snapshot replaces a market's book, every delta changes one price level (a level that reaches zero is dropped). Prices are dollars, counts are contracts; `yes` and `no` are the two bid books, the way Kalshi lists them (a NO bid at 0.70 is a YES ask at 0.30). A market the stream has not sent, or whose last message is older than max_age, has no book."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._book: dict[str, dict[str, dict[float, float]]] = {}
        self._at: dict[str, float] = {}
        self.enabled = False  # set by a stream that subscribed to the channel: KalshiClient.orderbook then reads here instead of REST

    def snapshot(self, ticker: str, yes: Sequence[tuple[float, float]], no: Sequence[tuple[float, float]], ts: float) -> None:
        with self._lock:
            self._book[ticker] = {"yes": {round(p, 4): c for p, c in yes if c > 0}, "no": {round(p, 4): c for p, c in no if c > 0}}
            self._at[ticker] = ts

    def delta(self, ticker: str, side: str, price: float, delta: float, ts: float) -> bool:
        """Apply one level change; False when the market has no snapshot yet (the change is not applied: the next snapshot will carry it)."""
        with self._lock:
            book = self._book.get(ticker)
            if book is None or side not in book:
                return False
            px = round(price, 4)
            left = book[side].get(px, 0.0) + delta
            if left > 1e-9:
                book[side][px] = left
            else:
                book[side].pop(px, None)
            self._at[ticker] = ts
            return True

    def drop(self, ticker: str) -> None:
        with self._lock:
            self._book.pop(ticker, None)
            self._at.pop(ticker, None)

    def clear(self) -> None:
        with self._lock:
            self._book.clear()
            self._at.clear()

    def age(self, ticker: str, now: float) -> float | None:
        with self._lock:
            at = self._at.get(ticker)
        return None if at is None else now - at

    def levels(self, ticker: str, side: str) -> list[tuple[float, float]] | None:
        """(price, contracts) of one bid book, best (highest price) first; None when the market has no book."""
        with self._lock:
            book = self._book.get(ticker)
            if book is None:
                return None
            return sorted(book[side].items(), reverse=True)

    def rest_book(self, ticker: str, now: float, max_age: float | None = None) -> dict[str, Any] | None:
        """The book in the shape of Kalshi's REST orderbook answer (strategy.best_bid, best_ask and side_ask_notional read it), or None when the stream has none for this market (the books are cleared when the connection drops or a sequence gap shows, so a book that exists is current; a quiet market sends nothing, so age is not a test unless max_age is given)."""
        age = self.age(ticker, now)
        if age is None or (max_age is not None and age > max_age):
            return None
        yes, no = self.levels(ticker, "yes") or [], self.levels(ticker, "no") or []
        return {"orderbook_fp": {"yes_dollars": [[f"{p:.4f}", f"{c:.2f}"] for p, c in sorted(yes)], "no_dollars": [[f"{p:.4f}", f"{c:.2f}"] for p, c in sorted(no)]}}



books = BookStore()
