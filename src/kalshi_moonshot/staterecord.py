"""State-record buying (2026-11-07, user): buy any curve state whose own recorded result says it pays, not one hand-picked pair of varieties.

scripts/state_record_walkforward.py (studies/results/2026-11-07-state-record-walkforward.txt): on the later days, buying the moments whose (5-minute variety, 60-minute variety) state had paid at least 0.20 per dollar staked on the earlier days, at that price band, beat the price (153 buys, +3.39 per $10 ticket, positive in both halves and both classes) and beat the fixed 60m D + 5m F rule it replaces. Only three test days existed, so this runs on paper first.
The state is the pair of letters (variants.scale_letter) at SCALES; the record is every settled market of the trailing days read at the lane's own entry window and price band, one reading per market side, state and price band, net of the taker fee, held to the result. The record is rebuilt daily from the tape in a separate low-priority process. The state never names a coin or an asset class.
A lane with record_buy reads the state at the buy and buys when the record, shrunk toward zero by K readings, is at least record_min per dollar with at least record_min_n earlier markets behind it.
"""

from __future__ import annotations

import bisect
import itertools
import json
import logging
import math
import os
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

log = logging.getLogger("moonshot")

SCALES = ("5m", "60m")
LOOK = {"5m": 300, "60m": 3600}
K = 10.0  # the shrink: a state with n readings counts its total over n + K
REFIT_EVERY = 86400.0
TRAIN_DAYS = 9.0
MOMENT_STEP = 30  # the record reads a market every 30 s
BANDS = (0.35, 0.5)  # price bands of the record: under 0.35, 0.35-0.5, 0.5 and over

_fitting: subprocess.Popen | None = None
_cache: dict[str, Any] = {"mtime": None, "data": None}


def band(ask: float) -> int:
    return 0 if ask < BANDS[0] else 1 if ask < BANDS[1] else 2


def state_key(letters: dict[str, str]) -> str | None:
    """The state of a bet, its letters at SCALES in order ("DF" = 5-minute D, 60-minute F); None when a scale is unread."""
    got = [letters.get(sc) for sc in SCALES]
    return None if any(x is None for x in got) else "".join(got)


def add_reading(cells: dict[str, list[float]], seen: set[tuple], market: str, side: str, state: str, ask: float, net_per_dollar: float) -> None:
    """One reading per market side, state and price band (the first moment, in time order)."""
    k = (market, side, state, band(ask))
    if k in seen:
        return
    seen.add(k)
    cell = cells.setdefault(f"{state}|{band(ask)}", [0, 0.0])
    cell[0] += 1
    cell[1] += net_per_dollar


def edge(record: dict[str, Any], state: str, ask: float) -> tuple[int, float]:
    """(markets behind the state at this price band, its shrunk net per dollar staked)."""
    n, total = record["cells"].get(f"{state}|{band(ask)}", (0, 0.0))
    return int(n), total / (n + K)


def load(s: Any) -> dict[str, Any] | None:
    p = Path(s.data_dir) / "state_record.json"
    try:
        m = p.stat().st_mtime
        if _cache["data"] is None or m != _cache["mtime"]:
            _cache["data"], _cache["mtime"] = json.loads(p.read_text()), m
    except (OSError, ValueError):
        return _cache["data"]
    return _cache["data"]


def admits(s: Any, store: Any, shot: Any, spec: Any, ask: float, waited: float = 0.0) -> tuple[bool, str]:
    """Whether a record_buy lane would buy this market side at `ask` now: (True, the note tag) or (False, why not). The state is read again from the in-memory ticks (the spot may have moved since the decision, `waited` seconds ago) and looked up in the record at the price asked."""
    from . import ticks as ticks_mod
    from . import variants

    try:
        ms = variants.multiscale_now(s, shot.series, shot.side, max(1.0, shot.hours_to_close * 3600.0 - waited), SCALES, strike=ticks_mod.strikes.get(shot.ticker) or store.window_strike(shot.ticker))
    except Exception as exc:
        log.warning("state record: varieties unreadable for %s %s: %s", shot.ticker, shot.side, exc)
        ms = None
    state = state_key({sc: v["variety"] for sc, v in ms.items()}) if ms else None
    if state is None:
        return False, f"state record: the varieties are unreadable for {shot.ticker}."
    record = load(s)
    if record is None:
        return False, f"state record: no record built yet (state {state})."
    n_state, net_state = edge(record, state, ask)
    tag = f"state {state}, record {net_state:+.2f} per $1 over {n_state} markets"
    if n_state >= spec.record_min_n and net_state >= spec.record_min:
        return True, tag
    return False, f"state record: {tag} is under the lane's {spec.record_min:g} over {spec.record_min_n} markets."


def maybe_refit(s: Any, now: float | None = None) -> bool:
    """Start the daily rebuild as its own low-priority process. True when one was started."""
    global _fitting
    now = time.time() if now is None else now
    if _fitting is not None and _fitting.poll() is None:
        return False
    rec = load(s)
    if rec and now - rec["built_at"] < REFIT_EVERY:
        return False
    log.info("state record: rebuilding from the tape (separate process)")
    _fitting = subprocess.Popen([sys.executable, "-m", "kalshi_moonshot.staterecord"], preexec_fn=lambda: os.nice(15))  # lowers the child's priority only
    return True


def _window_rules(specs: dict[str, Any]) -> dict[str, tuple[float, float, float, float, float]]:
    """Per series, the entry window (seconds in), price band and spread cap of the record_buy lane that trades it: the record reads the moments the lane could buy."""
    out: dict[str, tuple[float, float, float, float, float]] = {}
    for sp in specs.values():
        if not (sp.enabled and sp.record_buy):
            continue
        for series in sp.series:
            lo = max(sp.entry_elapsed[0] * 60.0, 900.0 - sp.hours[1] * 3600.0)  # the lane's own window: minutes in, and hours to the close
            hi = min(sp.entry_elapsed[1] * 60.0, 900.0 - sp.hours[0] * 3600.0)
            plo, phi = sp.price
            cap = sp.max_spread if sp.max_spread > 0 else 1.0
            prev = out.get(series)
            out[series] = (lo, hi, plo, phi, cap) if prev is None else (min(prev[0], lo), max(prev[1], hi), min(prev[2], plo), max(prev[3], phi), max(prev[4], cap))
    return out


def build(s: Any, now: float | None = None) -> dict[str, Any] | None:
    """The record from the tape: every settled window of the record_buy lanes' series in the trailing TRAIN_DAYS, read every MOMENT_STEP seconds inside the lane's entry window."""
    from . import db, variants
    from .kalshiws import RECORD_ONLY
    from .lanes import lane_specs
    from .store import Store
    from .strategy import fee_per_contract as fee

    now = time.time() if now is None else now
    rules = _window_rules(lane_specs(s))
    if not rules:
        return None
    store = Store(s.db_path, s.exclude_versions)
    tape = db.connect(s.tape_path, "tape", shared=True)
    t0 = now - TRAIN_DAYS * 86400.0
    g0 = int(t0 - 4300)
    g1 = int(now) + 2
    wins = store.db.execute("SELECT ticker,series,close_ts,strike,result FROM windows WHERE close_ts>? AND result IN ('yes','no') AND strike>0 ORDER BY close_ts", (t0,)).fetchall()
    cells: dict[str, list[float]] = defaultdict(lambda: [0, 0.0])
    cells = {}
    seen: set[tuple] = set()
    markets = 0
    grids: dict[str, tuple] = {}

    def grid(ix: str) -> tuple:
        if ix in grids:
            return grids[ix]
        grids.clear()  # one index at a time keeps the memory flat
        r = tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts>? AND value>0 ORDER BY ts", (ix, g0 - 60)).fetchall()
        ts = [float(x[0]) for x in r]
        vs = [float(x[1]) for x in r]
        g, j, last = [], 0, None
        for t in range(g0, g1):
            while j < len(ts) and ts[j] <= t:
                last = vs[j]
                j += 1
            g.append(last)
        sq = [0.0]
        for a, b in itertools.pairwise(g):
            sq.append(sq[-1] + (math.log(b / a) ** 2 if a and b else 0.0))
        grids[ix] = (ts, vs, g, sq)
        return grids[ix]

    def letter(z: float, slope: float, crosses: int, look: int) -> str:
        if crosses >= max(2, round(0.8 * look / 60.0)):
            return "G"
        if z >= variants.FAR:
            return "A" if slope >= variants.SLOPE else "C" if slope <= -variants.SLOPE else "B"
        return "D" if slope <= -variants.SLOPE else "F" if slope >= variants.SLOPE else "E"

    for w in sorted((w for w in wins if w["series"] in rules and w["series"] not in RECORD_ONLY), key=lambda w: (w["series"], w["close_ts"])):
        ser = w["series"]
        cx = variants.crypto_index(ser)
        ix = cx or variants.biquote_index(ser)
        if not ix:
            continue
        lo, hi, plo, phi, cap = rules[ser]
        close = float(w["close_ts"])
        opened = close - 900.0
        ts, vs, g, sq = grid(ix)
        ref = float(w["strike"])
        if not cx:  # a commodity is read against its own price when the window opened
            i0 = bisect.bisect_left(ts, opened)
            if i0 >= len(ts) or ts[i0] - opened > 20.0:
                continue
            ref = vs[i0]
        q = [(float(a), float(b), float(c)) for a, b, c in tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (w["ticker"], opened - 20, close)).fetchall()]
        if len(q) < 20:
            continue
        qt = [x[0] for x in q]
        ko = int(opened) - g0
        kstart = ko - 3700
        if kstart < 0 or int(close) - g0 >= len(g):
            continue
        cum = [0] * (int(close) - g0 - kstart + 2)
        prev = None
        for n, k in enumerate(range(kstart, int(close) - g0 + 1)):
            v = g[k]
            sgn = 1 if (v is not None and v > ref) else 0
            cum[n + 1] = cum[n] + (1 if (prev is not None and sgn != prev) else 0)
            prev = sgn
        markets += 1
        for e in range(max(60, int(lo) // MOMENT_STEP * MOMENT_STEP), min(841, int(hi) + 1), MOMENT_STEP):
            if e < lo:
                continue
            at = opened + e
            i = bisect.bisect_right(qt, at) - 1
            if i < 0 or at - qt[i] > 20:
                continue
            _, yb, ya = q[i]
            k = int(at) - g0
            if g[k] is None:
                continue
            left = close - at
            for side, bid, ask in (("yes", yb, ya), ("no", 1 - ya, 1 - yb)):
                if not (plo <= ask <= phi) or ask - bid > cap:
                    continue
                sg = 1 if side == "yes" else -1
                c = math.log(g[k] / ref) * sg
                lets: dict[str, str] = {}
                for sc in SCALES:
                    look = LOOK[sc]
                    rate = math.sqrt((sq[k] - sq[k - look]) / look)
                    if rate <= 0 or g[k - look] is None:
                        break
                    z = c / (rate * math.sqrt(left))
                    slope = (c - math.log(g[k - look] / ref) * sg) / (rate * math.sqrt(look))
                    crosses = cum[k - kstart + 1] - cum[k - look - kstart + 1]
                    lets[sc] = letter(z, slope, crosses, look)
                state = state_key(lets)
                if state is None:
                    continue
                won = w["result"] == side
                contracts = 1.0 / ask
                net = ((1 - ask) * contracts if won else -1.0) - fee(ask) * contracts
                add_reading(cells, seen, w["ticker"], side, state, ask, net)
    if not cells:
        return None
    return {"built_at": now, "days": TRAIN_DAYS, "markets": markets, "readings": sum(int(c[0]) for c in cells.values()), "cells": cells}


def main() -> None:
    from . import config

    s = config.reload()
    rec = build(s)
    if rec is None:
        print("no record built")
        return
    p = Path(s.data_dir) / "state_record.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(rec))
    tmp.replace(p)
    print("built", rec["markets"], "markets", rec["readings"], "readings", len(rec["cells"]), "cells")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
