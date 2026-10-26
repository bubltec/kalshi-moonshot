"""The scalp case memory: every attempt to buy a 30-48c side with a resting bid and sell it a few cents higher with a resting sell, replayed on the tape with what became of it.

An attempt is a moment (every 30 s, minute 1 to 10 of a finished 15-minute window) where a side's ask is 30-48c and its spread is 4c or less. The replay is the cautious one: the bid, at the side's bid, fills only if the
side's ask comes down to it within WAIT seconds; the resting sell, TARGET above the price paid, fills only if the bid reaches it within HOLD minutes (and before 30 s to the close). Neither leg pays a fee. A sell that did
not fill is sold at the bid with the taker fee (`ret_taker`) or held to the result (`ret_hold`). Each attempt is saved with the full feature snapshot, so a rule for when to skip can be learned from it and tested on later windows.

    python -m kalshi_moonshot scalp-cases     add the finished windows not yet replayed

Nothing here places an order.
"""

from __future__ import annotations

import bisect
import json
import logging

from . import db, factors, forecast
from .config import Settings
from .store import Store
from .strategy import fee_per_contract as fee

log = logging.getLogger("moonshot")

SERIES = ("KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M", "KXDOGE15M", "KXEURUSD15M", "KXGBPUSD15M", "KXUSDJPY15M", "KXAUDUSD15M", "KXUSDCAD15M",
          "KXGOLD15M", "KXSILVER15M", "KXWTI15M", "KXBRENT15M", "KXCOPPER15M", "KXNATGAS15M", "KXPLATINUM15M", "KXPALLADIUM15M")
BAND = (0.30, 0.48)
MAX_SPREAD = 0.04
TARGET = 0.03  # the resting sell sits this far above the price paid
WAIT = 120.0  # seconds the bid rests
HOLD = 4.0  # minutes the sell rests after the buy
STEP = 30.0


def build_cases(s: Settings, max_windows: int = 600) -> int:
    """Replay the finished windows that have no attempts saved yet. Returns how many attempts were added."""
    tape = db.connect(s.tape_path, "tape", shared=True)
    store = Store(s.db_path, s.exclude_versions)
    try:
        weights: forecast.Weights | None = forecast.load_weights(s.forecast_weights or f"{s.data_dir}/forecast_h5.json")
    except Exception:
        weights = None
    done = {r[0] for r in store.db.execute("SELECT DISTINCT ticker FROM scalp_cases").fetchall()}
    start = tape.execute("SELECT MIN(ts) FROM quotes").fetchone()[0] or 0.0
    marks = ",".join("?" * len(SERIES))
    windows = [w for w in store.db.execute(f"SELECT ticker, series, result, close_ts, strike FROM windows WHERE series IN ({marks}) AND result IN ('yes','no') AND close_ts > ? ORDER BY close_ts DESC", (*SERIES, start)).fetchall()
               if w["ticker"] not in done]
    added = 0
    for w in windows[:max_windows]:
        q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND bid > 0 AND ask > 0 ORDER BY ts", (w["ticker"],)).fetchall()
        close = float(w["close_ts"])
        if len(q) < 60:
            store.db.execute("INSERT OR IGNORE INTO scalp_cases (ticker, side, ts, series, close_ts, features) VALUES (?,?,?,?,?,?)", (w["ticker"], "-", close, w["series"], close, "{}"))
            continue
        qts = [r[0] for r in q]
        for side in ("yes", "no"):
            asks = [r[2] if side == "yes" else 1 - r[1] for r in q]
            bids = [r[1] if side == "yes" else 1 - r[2] for r in q]
            win = 1.0 if w["result"] == side else 0.0
            t = close - 900.0 + 60.0
            while t < close - 900.0 + 600.0:
                i = bisect.bisect_right(qts, t) - 1
                t += STEP
                if i < 0 or not (BAND[0] <= asks[i] <= BAND[1]) or asks[i] - bids[i] > MAX_SPREAD or bids[i] <= 0:
                    continue
                snap = factors.snapshot_full(tape, w["ticker"], side, close, qts[i], w["strike"], weights)
                if not snap:
                    continue
                px = bids[i]
                last = bisect.bisect_right(qts, min(qts[i] + WAIT, close - 60.0))
                f = next((j for j in range(i + 1, last) if asks[j] <= px + 1e-9), None)
                filled, hit, rt, rh = 0, None, None, None
                if f is not None:
                    filled = 1
                    end = min(qts[f] + HOLD * 60.0, close - 30.0)
                    kmax = bisect.bisect_right(qts, end)
                    g = next((k for k in range(f + 1, kmax) if bids[k] >= px + TARGET - 1e-9), None)
                    if g is not None:
                        hit, rt, rh = 1, TARGET / px, TARGET / px
                    else:
                        hit = 0
                        e = bids[max(f, kmax - 1)]
                        rt, rh = (e - fee(e) - px) / px, (win - px) / px
                store.db.execute("INSERT OR IGNORE INTO scalp_cases (ticker, side, ts, series, close_ts, features, filled, sell_hit, ret_taker, ret_hold) VALUES (?,?,?,?,?,?,?,?,?,?)",
                                 (w["ticker"], side, qts[i], w["series"], close, json.dumps(snap), filled, hit, rt, rh))
                added += 1
    store.db.commit()
    log.info("scalp: %d attempts saved", added)
    return added
