"""Every open 15-minute market's risk reading, written every 30 s.

2026-11-17, user: "We need to figure out what settings actually result in profit or loss on a market run, and if that attribute actually correlates. Are we recording every risk attribute now? If not we should record it".

Before this only a lane that buys on the score stored its reading, and only when one of its evaluations ran (one paper lane, almost all YES sides, about 200 rows an hour). Here a background thread reads all fifteen attributes (riskscore.NAMES, used in the sum or not) for every open market from the memory tape at each 30 s mark, with the YES bid and ask, and writes them to risk_readings in one batch. It is its own thread with its own store: no evaluation, buy or sale waits on it or reads it. A bet or a sale of any lane, live or paper, is joined to the reading at its 30 s mark by market and time, so nothing is stored per bet. risktune.py reads the table with the markets' results.

The whole market is read: no time gate and no price gate (2026-11-17, user: "Fully open ... No time or price gates so we can see what attributes are during the whole market"). The first minute and a price at 0.03 or 0.97 and beyond are recorded too, with the market's age so a study can cut by time. Only a market with no quote yet, or whose index is stale (over 30 s) or lacks its hour of history, has no row at a mark. The lanes' own reading keeps its limits, and risktune.py fits only the rows inside them.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import UTC, datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo

from . import riskscore

log = logging.getLogger(__name__)

EVERY = 30.0  # seconds between readings: the score's own look spacing (riskscore.HELD_LOOKS)
_ET = ZoneInfo("America/New_York")


def open_markets(s: Any, now: float) -> list[tuple[str, str, float]]:
    """(series, ticker, close time) of every 15-minute market open at `now`, built from the instruments and the quarter-hour (SERIES-YYMONDDHHMM-MM, the close in New York), as ticks.recent_15m_tickers does."""
    from .commodities import load_instruments

    close = (int(now // 900.0) + 1) * 900.0  # 900: a 15-minute market's length, structural
    t = datetime.fromtimestamp(close, UTC).astimezone(_ET)
    stamp = f"{t:%y}{t:%b}{t:%d}{t:%H%M}".upper()
    return [(f"{i.prefix}15M", f"{i.prefix}15M-{stamp}-{t:%M}", close) for i in load_instruments(s.data_dir) if "15M" in i.suffixes]


def snapshot(s: Any, now: float) -> list[tuple[str, float, str, float | None, float | None, float]]:
    """One row per readable open market: (ticker, the 30 s mark, the fifteen YES readings as JSON, YES bid, YES ask, the market's age in seconds). Memory only."""
    from . import ticks as ticks_mod

    mark = float(int(now // EVERY) * EVERY)
    out = []
    for series, ticker, close in open_markets(s, now):
        try:
            got = riskscore.live(series, ticker, "yes", close, now, None, (0.0,), whole=True)
        except Exception:
            log.exception("risk record: %s could not be read", ticker)
            continue
        if got is None:
            continue
        q = ticks_mod.mids.quotes(ticker)
        bid, ask = (q[-1][1], q[-1][2]) if q else (None, None)
        out.append((ticker, mark, json.dumps([round(v, 4) for v in got[3]]), bid, ask, round(now - (close - 900.0), 1)))
    return out


def start_background(get_settings: Callable[[], Any], get_store: Callable[[], Any], every: float = EVERY) -> None:
    """Write a snapshot at every mark from a background thread with its own store."""

    def loop() -> None:
        store, last_note = None, 0.0
        while True:
            now = time.time()
            time.sleep(every - now % every + 0.5)
            try:
                rows = snapshot(get_settings(), time.time())
                if rows:
                    store = store or get_store()
                    store.add_risk_readings(rows)
                if time.time() - last_note >= 900.0:
                    last_note = time.time()
                    log.info("risk record: %d open markets read at this mark", len(rows))
            except Exception:
                store = None
                log.exception("risk record: the mark was not written")

    threading.Thread(target=loop, name="risk-record", daemon=True).start()
