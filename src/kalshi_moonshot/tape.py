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
from pathlib import Path
from typing import Any

from . import db
from .commodities import instrument_for, load_instruments
from .config import Settings
from .kalshi import KalshiClient, KalshiError
from .kalshiws import KalshiStream, index_for_series

log = logging.getLogger("moonshot")

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
"""

FLUSH_EVERY = 5.0
REFRESH_EVERY = 20.0
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
            elif e["kind"] == "trade":
                row = self._flow.setdefault((int(e["ts"]), e["ticker"]), [0.0, 0.0, 0.0, 0.0])
                row[0 if e["taker"] == "yes" else 1] += e["count"]
                row[2] += 1
                row[3] += e["price"] * e["count"]
            elif e["kind"] == "index":
                self._index.append((e["ts"], e["index_id"], e["value"], e.get("avg60"), e.get("quarter_avg"),
                                    e.get("src_ms")))

    def add_feed(self, symbol: str, px: float, ts: float) -> None:
        with self._lock:
            prev = self._last_feed.get(symbol)
            if prev is not None and (prev[0] == px or ts - prev[1] < FEED_MIN_GAP):
                return
            self._last_feed[symbol] = (px, ts)
            self._feed.append((ts, symbol, px))

    def flush(self) -> int:
        with self._lock:
            q, i, f, fl = self._quotes, self._index, self._feed, self._flow
            self._quotes, self._index, self._feed, self._flow = [], [], [], {}
        if not (q or i or f or fl):
            return 0
        try:
            self.db.executemany("INSERT INTO quotes (ts, ticker, bid, ask, last, sts, bid_size, ask_size) VALUES (?,?,?,?,?,?,?,?)", q)
            self.db.executemany(
                "INSERT INTO flow_1s VALUES (?,?,?,?,?,?)",
                [(sec, tk, v[0], v[1], int(v[2]), (v[3] / (v[0] + v[1])) if (v[0] + v[1]) else None) for (sec, tk), v in fl.items()],
            )
            self.db.executemany("INSERT INTO index_ticks VALUES (?,?,?,?,?,?)", i)
            self.db.executemany("INSERT INTO feed_ticks VALUES (?,?,?)", f)
            self.db.commit()
        except Exception:
            self.db.rollback()
            with self._lock:  # the database was unreachable for a moment: keep these rows for the next flush instead of losing them
                self._quotes = q + self._quotes
                self._index = i + self._index
                self._feed = f + self._feed
                for key, v in fl.items():
                    mine = self._flow.setdefault(key, [0.0, 0.0, 0.0, 0.0])
                    for k in range(4):
                        mine[k] += v[k]
            raise
        return len(q) + len(i) + len(f) + len(fl)

    def prune(self, keep_days: float, now: float | None = None) -> None:
        cutoff = (now if now is not None else time.time()) - keep_days * 86400
        for table in ("quotes", "index_ticks", "feed_ticks", "flow_1s"):
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
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _on_event(self, e: dict[str, Any]) -> None:
        self.tape.add(e)
        for fn in self.listeners:
            try:
                fn(e)
            except Exception:
                log.exception("tape listener failed")

    def _series(self) -> list[str]:
        return [f"{i.prefix}15M" for i in load_instruments(self.s.data_dir) if "15M" in i.suffixes]

    def _commodity_symbols(self) -> list[str]:
        out = []
        for ser in self._series():
            inst = instrument_for(ser, load_instruments(self.s.data_dir))
            if inst and inst.symbol.startswith("hl:") and index_for_series(ser) is None:
                out.append(inst.symbol)
        return sorted(set(out))

    def refresh_tickers(self) -> int:
        tickers: set[str] = set()
        for ser in self._series():
            try:
                tickers.update(m["ticker"] for m in self.client.series_markets(ser) if m.get("status") in {"active", "open"})
            except KalshiError as exc:
                log.debug("tape: could not list %s (%s)", ser, exc)
        for ser in self.s.tape_extra_series:  # sports, esports and anything else named in TAPE_EXTRA_SERIES
            try:
                tickers.update(m["ticker"] for m in self.client.series_markets(ser) if m.get("status") in {"active", "open"})
            except KalshiError as exc:
                log.debug("tape: could not list %s (%s)", ser, exc)
        if self.extra_tickers is not None:
            try:
                tickers |= set(self.extra_tickers())
            except Exception as exc:
                log.debug("tape: extra tickers failed (%s)", exc)
        self.stream.set_tickers(tickers)
        return len(tickers)

    def follow(self, tickers: set[str]) -> None:
        """Start streaming these tickers now, without waiting for the next refresh (a position was just opened)."""
        current = set(self.stream._tickers)
        if not tickers <= current:
            self.stream.set_tickers(current | set(tickers))

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
        self._thread = threading.Thread(target=self._run, name="tape", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self.stream.close()

    def _run(self) -> None:
        last_refresh = last_flush = last_prune = 0.0
        while not self._stop.is_set():
            now = time.time()
            try:
                if now - last_refresh >= REFRESH_EVERY:
                    self.refresh_tickers()
                    last_refresh = now
                self.sample_feed()
                if now - last_flush >= FLUSH_EVERY:
                    self.tape.flush()
                    last_flush = now
                if now - last_prune >= PRUNE_EVERY:
                    self.tape.prune(self.s.tape_keep_days)
                    last_prune = now
            except Exception as exc:
                log.warning("tape: %s", exc)
            self._stop.wait(1.0)
