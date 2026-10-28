"""A timestamped tape of the 15-minute contracts next to the price they follow.

For every open 15-minute market it records each change of the Kalshi quote, together with the
settlement index it tracks (BRTI and friends, straight from Kalshi) or, for commodities, the
Hyperliquid price. `leadlag.py` then asks whether the quote reacts to a move in the underlying
instantly or a few seconds late, and whether that delay is large enough to trade.

Rows go to their own schema (or file, for SQLite), so the busy stream never contends with the bot's own tables.
Nothing here places an order.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from . import archive, db
from .coinbasefeed import DEFAULT_PRODUCTS, CoinbaseFeed
from .commodities import instrument_for, load_instruments
from .config import Settings
from .futflow import FuturesFlow
from .kalshi import KalshiClient, KalshiError
from .kalshiws import KalshiStream, index_for_series
from .store import Store

log = logging.getLogger("moonshot")

FX_PAIRS = ("EUR/USD", "GBP/USD", "USD/JPY", "AUD/USD", "USD/CAD")
PAIR_PREFIX = "kraken:"

SCHEMA = """
CREATE TABLE IF NOT EXISTS quotes (
  ts REAL NOT NULL, ticker TEXT NOT NULL, bid REAL, ask REAL, last REAL, sts INTEGER
);
CREATE INDEX IF NOT EXISTS idx_quotes ON quotes(ticker, ts);
CREATE TABLE IF NOT EXISTS index_ticks (
  ts REAL NOT NULL, index_id TEXT NOT NULL, value REAL, avg60 REAL, quarter_avg REAL, src_ms INTEGER
);
CREATE INDEX IF NOT EXISTS idx_index_ticks ON index_ticks(index_id, ts);
CREATE TABLE IF NOT EXISTS flow_1s (
  ts INTEGER NOT NULL, ticker TEXT NOT NULL,
  yes_vol REAL NOT NULL, no_vol REAL NOT NULL,  -- contracts bought by takers on each side during this second
  n INTEGER NOT NULL, vwap REAL                 -- trades, and their average YES price
);
CREATE INDEX IF NOT EXISTS idx_flow ON flow_1s(ticker, ts);
CREATE TABLE IF NOT EXISTS feed_ticks (ts REAL NOT NULL, symbol TEXT NOT NULL, px REAL NOT NULL);
CREATE INDEX IF NOT EXISTS idx_feed_ticks ON feed_ticks(symbol, ts);
CREATE TABLE IF NOT EXISTS fut_1s (
  ts INTEGER NOT NULL, symbol TEXT NOT NULL,
  px REAL NOT NULL,                                -- last trade price in this second (a perpetual future, not spot)
  buy_usd REAL NOT NULL, sell_usd REAL NOT NULL,   -- dollars bought and sold by takers (aggressors) during this second
  buy_n INTEGER NOT NULL, sell_n INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fut_1s ON fut_1s(symbol, ts);
"""

FLUSH_EVERY = 5.0
RECENT_SECONDS = 60.0  # the live snapshot: every quote and feed price still in this window, kept after the database flush
STOP_SECONDS = 900.0  # a market being watched keeps this much quote history, loaded once when the watch starts
INDEX_SECONDS = 1860.0  # a settlement index keeps this much, so the last-minute chance can be computed without a query


class Recent:
    """The last 60 seconds of the tape, in memory. Readers use it instead of the database when it already covers the question."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._quotes: dict[str, deque[tuple[float, float, float]]] = {}
        self._feed: dict[str, deque[tuple[float, float]]] = {}
        self._stop: dict[str, tuple[float, deque[tuple[float, float, float]]]] = {}  # ticker -> (loaded from, quotes since)
        self._feed_watch: dict[str, tuple[float, deque[tuple[float, float]]]] = {}  # symbol -> (loaded from, prices since)
        self._index: dict[str, tuple[float, deque[tuple[float, float]]]] = {}  # index id -> (loaded from, ticks since)
        self._index_ready: set[str] = set()

    def clear(self) -> None:
        with self._lock:
            self._quotes.clear()
            self._feed.clear()
            self._stop.clear()
            self._feed_watch.clear()
            self._index.clear()
            self._index_ready.clear()

    def note_quote(self, ticker: str, ts: float, bid: float, ask: float) -> None:
        row = (float(ts), float(bid), float(ask))
        self._note(self._quotes, ticker, row, row[0])
        self._extend_stop(ticker, row)

    def stop_armed(self, ticker: str) -> bool:
        with self._lock:
            return ticker in self._stop

    def arm_stop(self, ticker: str, loaded_from: float, rows: list[tuple[float, float, float]]) -> None:
        """Keep `rows` as this ticker's stop history. A second call leaves the first one in place."""
        with self._lock:
            if ticker in self._stop:
                return
            by = {row[0]: row for row in rows}
            for row in self._quotes.get(ticker, ()):
                by[row[0]] = row
            kept: deque[tuple[float, float, float]] = deque(by[k] for k in sorted(by))
            if kept:
                cutoff = kept[-1][0] - STOP_SECONDS
                while kept and kept[0][0] < cutoff:
                    kept.popleft()
            self._stop[ticker] = (float(loaded_from), kept)

    def stop_quotes(self, ticker: str, start: float, end: float) -> list[tuple[float, float, float]] | None:
        """This watch's quotes with start < ts <= end. None when the watch was never loaded, or it does not reach `start`."""
        with self._lock:
            slot = self._stop.get(ticker)
            if slot is None:
                return None
            loaded_from, rows = slot
            if loaded_from > start:
                return None
            return [row for row in rows if start < row[0] <= end and row[1] > 0 and row[2] > 0]

    def _extend_stop(self, ticker: str, row: tuple[float, float, float]) -> None:
        with self._lock:
            slot = self._stop.get(ticker)
            if slot is None:
                return
            loaded_from, rows = slot
            rows.append(row)
            cutoff = row[0] - STOP_SECONDS
            while rows and rows[0][0] < cutoff:
                rows.popleft()
            self._stop[ticker] = (max(loaded_from, cutoff), rows)

    def feed_armed(self, symbol: str) -> bool:
        with self._lock:
            return symbol in self._feed_watch

    def arm_feed(self, symbol: str, loaded_from: float, rows: list[tuple[float, float]]) -> None:
        with self._lock:
            if symbol in self._feed_watch:
                return
            by = {row[0]: row for row in rows}
            for row in self._feed.get(symbol, ()):
                by[row[0]] = row
            kept: deque[tuple[float, float]] = deque(by[k] for k in sorted(by))
            self._feed_watch[symbol] = (float(loaded_from), kept)

    def feed_rows(self, symbol: str, start: float, end: float) -> list[tuple[float, float]] | None:
        with self._lock:
            slot = self._feed_watch.get(symbol)
            if slot is None:
                return None
            loaded_from, rows = slot
            if loaded_from > start:
                return None
            return [row for row in rows if start < row[0] <= end]

    def index_armed(self, index_id: str) -> bool:
        with self._lock:
            return index_id in self._index_ready

    def arm_index(self, index_id: str, loaded_from: float, rows: list[tuple[float, float]]) -> None:
        with self._lock:
            if index_id in self._index_ready:
                return
            by = {row[0]: row for row in rows}
            slot = self._index.get(index_id)
            if slot is not None:
                for row in slot[1]:
                    by[row[0]] = row
            kept: deque[tuple[float, float]] = deque(by[k] for k in sorted(by))
            self._index[index_id] = (float(loaded_from), kept)
            self._index_ready.add(index_id)

    def index_rows(self, index_id: str, start: float, end: float) -> list[tuple[float, float]] | None:
        with self._lock:
            if index_id not in self._index_ready:
                return None
            slot = self._index.get(index_id)
            if slot is None:
                return None
            loaded_from, rows = slot
            if loaded_from > start:
                return None
            return [row for row in rows if start < row[0] <= end]

    def _extend_pair(self, table: dict[str, tuple[float, deque]], key: str, row: tuple[float, float], keep: float) -> None:
        with self._lock:
            slot = table.get(key)
            if slot is None:
                return
            loaded_from, rows = slot
            rows.append(row)
            cutoff = row[0] - keep
            while rows and rows[0][0] < cutoff:
                rows.popleft()
            table[key] = (max(loaded_from, cutoff), rows)

    def note_feed(self, symbol: str, ts: float, px: float) -> None:
        row = (float(ts), float(px))
        self._note(self._feed, symbol, row, row[0])
        self._extend_pair(self._feed_watch, symbol, row, STOP_SECONDS)

    def note_index(self, index_id: str, ts: float, value: float) -> None:
        """Keep the tick even before the history load, so nothing that arrives during that load is missed."""
        row = (float(ts), float(value))
        with self._lock:
            slot = self._index.get(index_id)
            if slot is None:
                self._index[index_id] = (row[0], deque([row]))
                return
            loaded_from, rows = slot
            rows.append(row)
            cutoff = row[0] - INDEX_SECONDS
            while rows and rows[0][0] < cutoff:
                rows.popleft()
            self._index[index_id] = (max(loaded_from, cutoff), rows)

    def _note(self, table: dict[str, deque], key: str, row: tuple, ts: float) -> None:
        cutoff = ts - RECENT_SECONDS
        with self._lock:
            rows = table.get(key)
            if rows is None:
                rows = deque()
                table[key] = rows
            rows.append(row)
            while rows and rows[0][0] < cutoff:
                rows.popleft()

    def prune(self, now: float) -> None:
        cutoff = now - RECENT_SECONDS
        with self._lock:
            for table in (self._quotes, self._feed):
                dead = [key for key, rows in table.items() if not rows or rows[-1][0] < cutoff]
                for key in dead:
                    del table[key]
            quiet = [key for key, (_loaded, rows) in self._stop.items() if not rows or rows[-1][0] < now - STOP_SECONDS]
            for key in quiet:
                del self._stop[key]

    def quotes(self, ticker: str, start: float, end: float) -> list[tuple[float, float, float]] | None:
        """Quotes with start < ts <= end. None when this snapshot does not reach `start` and the database may know more."""
        return self._slice(self._quotes, ticker, start, end)

    def feed(self, symbol: str, start: float, end: float) -> list[tuple[float, float]] | None:
        """Feed prices with start < ts <= end. None when the snapshot does not reach `start`."""
        return self._slice(self._feed, symbol, start, end)

    def latest_quote(self, ticker: str, start: float, end: float) -> tuple[float, float, float] | None:
        """The newest quote with start < ts <= end. None when the snapshot covers that window and it is empty. MISS when it does not, so the caller reads the database."""
        with self._lock:
            rows = self._quotes.get(ticker)
            if not rows:
                return MISS
            found = None
            for row in rows:
                if start < row[0] <= end and row[1] > 0 and row[2] > 0:
                    found = row
            if found is not None or rows[0][0] <= start:
                return found
            return MISS

    def quote_tail(self, ticker: str, start: float, end: float) -> list[tuple[float, float, float]]:
        """Quotes in range that the snapshot holds, even when it does not reach `start`."""
        with self._lock:
            return [row for row in self._quotes.get(ticker, ()) if start < row[0] <= end]

    def feed_tail(self, symbol: str, start: float, end: float) -> list[tuple[float, float]]:
        with self._lock:
            return [row for row in self._feed.get(symbol, ()) if start < row[0] <= end]

    def _slice(self, table: dict[str, deque], key: str, start: float, end: float) -> list | None:
        with self._lock:
            rows = table.get(key)
            if not rows:
                return None
            if rows[0][0] > start:
                return None  # the snapshot starts after the question does, so the database may have the older part
            return [row for row in rows if start < row[0] <= end]


recent = Recent()
MISS = object()  # the snapshot has no answer, and the database might
REFRESH_EVERY = 20.0  # how often the refresh loop looks at which series are due (each call lists only the series that are)
SERIES_EVERY = 20.0  # a series with markets open is listed again this often: a new 15-minute window opens every 15 minutes
IDLE_EVERY = 120.0  # a series with nothing open (a closed session) is looked at this often
EXTRA_EVERY = 90.0  # sports and esports series list their matches hours ahead, so they need no faster
EXTRA_IDLE_EVERY = 300.0
PRUNE_EVERY = 3600.0
FEED_MIN_GAP = 0.5  # at most one stored price per symbol per this many seconds


class Tape:
    def __init__(self, path: Path | str):
        self.db = db.connect(path, "tape", shared=True)
        if not db.is_pg(self.db):
            self.db.execute("PRAGMA journal_mode=WAL")  # readers never block the stream, and a crash mid-write cannot damage the file
            self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        for col in ("bid_size", "ask_size"):  # an older tape file has no top-of-book sizes
            if col not in {r[1] for r in self.db.execute("PRAGMA table_info(quotes)")}:
                self.db.execute(f"ALTER TABLE quotes ADD COLUMN {col} REAL")
        self.db.commit()
        self._lock = threading.Lock()
        self._quotes: list[tuple] = []
        self._index: list[tuple] = []
        self._feed: list[tuple] = []
        self._flow: dict[tuple[int, str], list[float]] = {}  # (second, ticker) -> [yes_vol, no_vol, n, price*count]
        self._fut: dict[tuple[int, str], list[float]] = {}  # (second, coin) -> [last px, buy usd, sell usd, buy n, sell n]
        self._last_quote: dict[str, tuple] = {}
        self._last_feed: dict[str, tuple[float, float]] = {}

    def add(self, e: dict[str, Any]) -> None:
        """Queue one event from the stream. A repeated quote (same bid and ask) is skipped."""
        with self._lock:
            if e["kind"] == "quote":
                key = (e["bid"], e["ask"], round(e.get("bid_size") or 0), round(e.get("ask_size") or 0))
                prev = self._last_quote.get(e["ticker"])
                if prev is not None and prev == key:
                    return
                self._last_quote[e["ticker"]] = key
                self._quotes.append((e["ts"], e["ticker"], e["bid"], e["ask"], e.get("last"), e.get("sts"),
                                     e.get("bid_size"), e.get("ask_size")))
                recent.note_quote(e["ticker"], e["ts"], e["bid"], e["ask"])
            elif e["kind"] == "trade":
                row = self._flow.setdefault((int(e["ts"]), e["ticker"]), [0.0, 0.0, 0.0, 0.0])
                row[0 if e["taker"] == "yes" else 1] += e["count"]
                row[2] += 1
                row[3] += e["price"] * e["count"]
            elif e["kind"] == "index":
                self._index.append((e["ts"], e["index_id"], e["value"], e.get("avg60"), e.get("quarter_avg"),
                                    e.get("src_ms")))
                recent.note_index(e["index_id"], e["ts"], e["value"])

    def add_fut(self, coin: str, ts: float, px: float, size: float, bought: bool) -> None:
        """One live perpetual-futures trade: folded into its second's row (see futflow.py)."""
        with self._lock:
            row = self._fut.setdefault((int(ts), coin), [px, 0.0, 0.0, 0.0, 0.0])
            row[0] = px
            row[1 if bought else 2] += px * size
            row[3 if bought else 4] += 1

    def add_feed(self, symbol: str, px: float, ts: float, min_gap: float = FEED_MIN_GAP) -> None:
        with self._lock:
            prev = self._last_feed.get(symbol)
            if prev is not None and (prev[0] == px or ts - prev[1] < min_gap):
                return
            self._last_feed[symbol] = (px, ts)
            self._feed.append((ts, symbol, px))
            recent.note_feed(symbol, ts, px)

    def flush(self, now: float | None = None) -> int:
        cutoff = int((now if now is not None else time.time()) - 2)  # a second still being filled stays in memory, so each (second, coin) is one row
        with self._lock:
            q, i, f, fl = self._quotes, self._index, self._feed, self._flow
            self._quotes, self._index, self._feed, self._flow = [], [], [], {}
            done = {k: v for k, v in self._fut.items() if k[0] <= cutoff}
            for finished in done:
                del self._fut[finished]
        ft = [(sec, coin, v[0], v[1], v[2], int(v[3]), int(v[4])) for (sec, coin), v in done.items()]
        if not (q or i or f or fl or ft):
            return 0
        try:
            self.db.executemany("INSERT INTO quotes (ts, ticker, bid, ask, last, sts, bid_size, ask_size) VALUES (?,?,?,?,?,?,?,?)", q)
            self.db.executemany(
                "INSERT INTO flow_1s VALUES (?,?,?,?,?,?)",
                [(sec, tk, v[0], v[1], int(v[2]), (v[3] / (v[0] + v[1])) if (v[0] + v[1]) else None) for (sec, tk), v in fl.items()],
            )
            self.db.executemany("INSERT INTO index_ticks VALUES (?,?,?,?,?,?)", i)
            self.db.executemany("INSERT INTO feed_ticks VALUES (?,?,?)", f)
            self.db.executemany("INSERT INTO fut_1s VALUES (?,?,?,?,?,?,?)", ft)
            self.db.commit()
            recent.prune(now if now is not None else time.time())
        except Exception:
            self.db.rollback()
            with self._lock:  # the database was unreachable for a moment: keep these rows for the next flush instead of losing them
                self._quotes = q + self._quotes
                self._index = i + self._index
                self._feed = f + self._feed
                for sec, coin, px, bu, su, bn, sn in ft:  # put the futures seconds back, merging with any trades that arrived since
                    mine = self._fut.setdefault((sec, coin), [px, 0.0, 0.0, 0.0, 0.0])
                    mine[1] += bu
                    mine[2] += su
                    mine[3] += bn
                    mine[4] += sn
                for key, v in fl.items():
                    mine = self._flow.setdefault(key, [0.0, 0.0, 0.0, 0.0])
                    for k in range(4):
                        mine[k] += v[k]
            raise
        return len(q) + len(i) + len(f) + len(fl) + len(ft)

    def prune(self, keep_days: float, now: float | None = None) -> None:
        cutoff = (now if now is not None else time.time()) - keep_days * 86400
        for table in ("quotes", "index_ticks", "feed_ticks", "flow_1s", "fut_1s"):
            self.db.execute(f"DELETE FROM {table} WHERE ts < ?", (cutoff,))
        self.db.commit()


class TapeRecorder:
    """Runs the stream and a small maintenance loop: which markets to follow, flushing, pruning."""

    def __init__(self, client: KalshiClient, s: Settings, feed: Any = None):
        self.client, self.s, self.feed = client, s, feed
        self.tape = Tape(s.tape_path)
        self.extra_tickers: Any = None  # a callable returning more tickers to stream (positions we hold in any market)
        self.listeners: list[Any] = []  # called with every event after it is stored (must be fast and never raise)
        self.stream = KalshiStream(client, self._on_event)
        # Sports and esports series list a new market every few minutes, and a changed market list reconnects a stream. They get their own connection so the
        # live one (15-minute quotes, settlement indexes, the account's fills) is never dropped for them.
        self.extra_stream = KalshiStream(client, self._on_event, index_feed=False, fills=False)
        self.coinbase = CoinbaseFeed(DEFAULT_PRODUCTS, lambda product, ts, px: self.tape.add_feed(f"coinbase:{product}", px, ts, 1.0)) if s.coinbase_feed_enabled else None
        self.futflow = FuturesFlow(s.fut_flow_coins, self.tape.add_fut) if s.fut_flow_enabled and s.fut_flow_coins else None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._refresh_thread: threading.Thread | None = None
        self._listed: dict[str, tuple[float, set[str]]] = {}  # series -> (when it was listed, its open tickers)

    def _on_event(self, e: dict[str, Any]) -> None:
        self.tape.add(e)
        cache = getattr(self.client, "cache", None)
        if cache is not None and e.get("kind") == "quote":
            cache.note_quote(e["ticker"], e.get("bid"), e.get("ask"), e.get("ts"))  # the shared market cache's prices stay as fresh as the stream
        for fn in self.listeners:
            try:
                fn(e)
            except Exception:
                log.exception("tape listener failed")

    def _series(self) -> list[str]:
        from .runner import watched  # late: runner imports modules that import the tape
        keep = watched(self.s)
        names = [f"{i.prefix}15M" for i in load_instruments(self.s.data_dir) if "15M" in i.suffixes]
        return names if keep is None else [n for n in names if n in keep]

    def _commodity_symbols(self) -> list[str]:
        out = []
        for ser in self._series():
            inst = instrument_for(ser, load_instruments(self.s.data_dir))
            if inst and inst.symbol.startswith("hl:") and index_for_series(ser) is None:
                out.append(inst.symbol)
        return sorted(set(out))

    def _list(self, series: str, every_open: float, every_idle: float, now: float) -> set[str]:
        """The open tickers of one series: the last listing, unless it is due again. Each listing is a request on the bot's shared, rate-limited client."""
        seen = self._listed.get(series)
        if seen is not None and now - seen[0] < (every_open if seen[1] else every_idle):
            return seen[1]
        try:
            found = {m["ticker"] for m in self.client.series_markets(series) if m.get("status") in {"active", "open"}}
        except KalshiError as exc:
            log.debug("tape: could not list %s (%s)", series, exc)
            return seen[1] if seen else set()
        self._listed[series] = (now, found)
        return found

    def refresh_tickers(self, now: float | None = None) -> int:
        now = time.time() if now is None else now
        tickers: set[str] = set()
        for ser in self._series():
            tickers |= self._list(ser, SERIES_EVERY, IDLE_EVERY, now)
        extras: set[str] = set()
        for ser in self.s.tape_extra_series:  # sports, esports and anything else named in TAPE_EXTRA_SERIES
            extras |= self._list(ser, EXTRA_EVERY, EXTRA_IDLE_EVERY, now)
        if self.extra_tickers is not None:
            try:
                tickers |= set(self.extra_tickers())
            except Exception as exc:
                log.debug("tape: extra tickers failed (%s)", exc)
        self.stream.set_tickers(tickers)
        self.extra_stream.set_tickers(extras - tickers)  # a market the live stream already carries is not recorded twice
        self._arm(tickers | extras, now)
        return len(tickers) + len(extras - tickers)

    def follow(self, tickers: set[str]) -> None:
        """Start streaming these tickers now, without waiting for the next refresh (a position was just opened)."""
        current = set(self.stream._tickers)
        if not tickers <= current:
            self.stream.set_tickers(current | set(tickers))
        self._arm(set(tickers), time.time())

    def _arm(self, tickers: set[str], now: float) -> None:
        """Load each market's history once, on this thread, so a later bid or sale does not wait on the database."""
        from .runner import _spot_symbols, arm_feed_history, arm_index_history, arm_stop_history

        for ticker in tickers:
            arm_stop_history(self.s, ticker, now)
        symbols: set[str] = set()
        indexes: set[str] = set()
        for ser in self._series():
            idx = index_for_series(ser)
            if idx:
                indexes.add(idx)
            symbols.update(_spot_symbols(ser))
        for sym in self._commodity_symbols():
            symbols.add(sym)
        for symbol in symbols:
            arm_feed_history(self.s, symbol, now)
        for index_id in indexes:
            arm_index_history(self.s, index_id, now)

    def _crypto_symbols(self) -> list[str]:
        """Kraken spot symbols behind the crypto 15-minute markets. Kalshi settles on the BRTI-style index; Kraken's own trades are a faster look at the same price."""
        out = []
        for ser in self._series():
            inst = instrument_for(ser, load_instruments(self.s.data_dir))
            if inst and inst.symbol.startswith("kraken:") and index_for_series(ser) is not None:
                out.append(inst.symbol)
        return sorted(set(out))

    def sample_kraken(self) -> None:
        """Store Kraken's latest price for each crypto symbol beside the index ticks, to test whether a faster exchange price leads the quote."""
        kr = getattr(self.feed, "kraken", None)
        if kr is None:
            return
        for sym in self._crypto_symbols():
            got = kr.latest(sym)
            if got is not None:
                self.tape.add_feed(sym, got[0], got[1])
        # The currency 15-minute markets settle on ONE Pyth price at the close (not an average); Pyth's own feed is closed to us, so Kraken's spot for the same pairs is recorded once a second as `krakenfx:EUR/USD` etc.
        # to test whether spot against the strike tells a collapse in the last minutes (scripts/fx_guard_study.py once a few days are in).
        for pair in FX_PAIRS:
            sym = PAIR_PREFIX + pair
            got = kr.latest(sym)
            if got is None:  # first use subscribes the pair; the next sample finds its price
                try:
                    kr.series(sym)
                except Exception:
                    continue
                got = kr.latest(sym)
            if got is not None:
                self.tape.add_feed("krakenfx:" + pair, got[0], got[1])

    def sample_feed(self) -> None:
        self.sample_kraken()
        hl = getattr(self.feed, "hyperliquid", None)
        if hl is None:
            return
        for sym in self._commodity_symbols():
            got = hl.latest(sym)
            if got is None:  # first use subscribes the coin; the next sample finds its price
                try:
                    hl.series(sym)
                except Exception:
                    continue
                got = hl.latest(sym)
            if got is not None:
                self.tape.add_feed(sym, got[0], got[1])

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self.stream.start()
        if self.s.tape_extra_series:  # no extra series named: no second connection
            self.extra_stream.start()
        if self.futflow is not None:
            self.futflow.start()
        if self.coinbase is not None:
            self.coinbase.start()
        self._thread = threading.Thread(target=self._run, name="tape", daemon=True)
        self._thread.start()
        # Listing the series is slow (a request each, on a shared client): it runs on its own thread so it can never hold up the writes below.
        self._refresh_thread = threading.Thread(target=self._refresh_loop, name="tape-refresh", daemon=True)
        self._refresh_thread.start()

    def stop(self) -> None:
        self._stop.set()
        self.stream.close()
        self.extra_stream.close()
        if self.futflow is not None:
            self.futflow.close()
        if self.coinbase is not None:
            self.coinbase.close()

    def _refresh_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.refresh_tickers()
            except Exception as exc:
                log.warning("tape: %s", exc)
            self._stop.wait(REFRESH_EVERY)

    def _run(self) -> None:
        last_flush = last_prune = 0.0
        while not self._stop.is_set():
            now = time.time()
            try:
                self.sample_feed()
                if now - last_flush >= FLUSH_EVERY:
                    self.tape.flush()
                    last_flush = now
                if now - last_prune >= PRUNE_EVERY:
                    if archive.enabled(self.s):  # old rows leave only after they are verified in the bucket (and only if ARCHIVE_DELETE_LOCAL is on)
                        archive.prune_local(self.s, Store(self.s.db_path, self.s.exclude_versions), self.tape.db)
                    else:
                        self.tape.prune(self.s.tape_keep_days)
                    last_prune = now
            except Exception as exc:
                log.warning("tape: %s", exc)
            self._stop.wait(1.0)
