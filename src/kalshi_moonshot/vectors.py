"""The vector memory: every finished 15-minute crypto window, at each decision minute, as a point made of the curves around it, with what became of it.

A vector point has five parts (all in the tape's own units, measured from the window's target, its opening index value, in the coin's usual per-minute move `v1`, the standard deviation of the previous 240 minutes' returns):
  coin    the index at the end of each whole minute since the open (m values)
  fine    the index every 10 seconds over the last 2 minutes before the decision (12 values): the short wiggles
  ctx     the index at points 5-60 minutes and 90-180 minutes before the open (11 values, scaled by the square root of the minutes back)
  kal     Kalshi's YES mid at the end of each minute since the open (m values)
  kfine   Kalshi's YES mid every 10 seconds over the last 2 minutes (12 values)
With the YES bid and ask at the decision, whether the window finished above its target, and whether a resting sell 3c above the price paid would have filled within 3 minutes (either side). Each part is a curve, so a "feature" here is a curve's behavior at that scale;
a memory of these points, one table per coin, is what an analog (nearest-neighbour) forecast reads. The table grows by itself: the recorder holds 1-second index ticks and quotes, and this adds every finished window. Nothing here places an order.

    python -m kalshi_moonshot curve-vectors      add the finished windows not yet saved
"""

from __future__ import annotations

import bisect
import itertools
import json
import logging
import math

from . import db
from .config import Settings
from .forecast_fit import CRYPTO
from .kalshiws import index_for_series
from .store import Store

log = logging.getLogger("moonshot")

MINUTES = range(5, 13)
HOUR = (5, 10, 15, 20, 30, 45, 60)
THREE = (90, 120, 150, 180)
TARGET = 0.03
LATER = 180.0


def build(s: Settings, max_windows: int = 800) -> int:
    """Add the finished windows that have no vector points yet. Returns how many points were added."""
    tape = db.connect(s.tape_path, "tape", shared=True)
    store = Store(s.db_path, s.exclude_versions)
    done = {r[0] for r in store.db.execute("SELECT DISTINCT ticker FROM curve_vectors").fetchall()}
    start = tape.execute("SELECT MIN(ts) FROM index_ticks").fetchone()[0] or 0.0
    marks = ",".join("?" * len(CRYPTO))
    wins = [w for w in store.db.execute(f"SELECT ticker, series, result, close_ts FROM windows WHERE series IN ({marks}) AND result IN ('yes','no') AND close_ts - 900 - 15000 > ? ORDER BY close_ts DESC", (*CRYPTO, start)).fetchall()
            if w["ticker"] not in done]
    ticks: dict[str, tuple[list[float], list[float]]] = {}
    added = 0
    for w in wins[:max_windows]:
        index_id = index_for_series(w["series"])
        if index_id is None:
            continue
        if index_id not in ticks:
            r = tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND value > 0 ORDER BY ts", (index_id,)).fetchall()
            ticks[index_id] = ([x[0] for x in r], [x[1] for x in r])
        its, ivs = ticks[index_id]
        q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND bid > 0 AND ask > 0 ORDER BY ts", (w["ticker"],)).fetchall()
        if len(q) < 60:
            continue
        qts = [x[0] for x in q]
        close = float(w["close_ts"])
        o = close - 900.0
        y = 1 if w["result"] == "yes" else 0

        def idx_at(t: float, its=its, ivs=ivs) -> float | None:
            i = bisect.bisect_right(its, t) - 1
            return ivs[i] if i >= 0 and t - its[i] < 30.0 else None

        def mid_at(t: float, q=q, qts=qts) -> float | None:
            i = bisect.bisect_right(qts, t) - 1
            return (q[i][1] + q[i][2]) / 2 if i >= 0 and t - qts[i] < 45.0 else None

        open_v = idx_at(o)
        before = [idx_at(o - 60 * k) for k in range(240, -1, -1)]
        if open_v is None or None in before:
            continue
        known = [b for b in before if b is not None]
        rets = [math.log(b / a) for a, b in itertools.pairwise(known)]
        mean = sum(rets) / len(rets)
        v1 = math.sqrt(max(sum((r - mean) ** 2 for r in rets) / len(rets), 1e-14))
        ctx = [idx_at(o - 60 * j) for j in HOUR + THREE]
        if None in ctx:
            continue
        ctxv = [math.log(float(c) / open_v) / (v1 * math.sqrt(j + 8)) for c, j in zip(ctx, HOUR + THREE, strict=True) if c is not None]
        for m in MINUTES:
            t_dec = o + 60.0 * m
            coin = [idx_at(o + 60 * k) for k in range(1, m + 1)]
            fine = [idx_at(t_dec - 120 + 10 * j) for j in range(1, 13)]
            kal = [mid_at(o + 60 * k) for k in range(1, m + 1)]
            kfine = [mid_at(t_dec - 120 + 10 * j) for j in range(1, 13)]
            iq = bisect.bisect_right(qts, t_dec) - 1
            if None in coin or None in fine or None in kal or None in kfine or iq < 0 or t_dec - qts[iq] > 45.0:
                continue
            bid, ask = q[iq][1], q[iq][2]
            j2 = bisect.bisect_right(qts, min(t_dec + LATER, close - 5))
            later = q[iq + 1:j2]
            hit_yes = int(any(x[1] >= ask + TARGET - 1e-9 for x in later))  # the YES bid reaches the YES ask paid plus 3c
            hit_no = int(any((1 - x[2]) >= (1 - bid) + TARGET - 1e-9 for x in later))  # the NO bid (1 - the YES ask) reaches the NO ask (1 - the YES bid) paid plus 3c
            vec = {"coin": [round(math.log(float(c) / open_v) / v1, 4) for c in coin if c is not None], "fine": [round(math.log(float(f) / open_v) / v1, 4) for f in fine if f is not None],
                   "ctx": [round(c, 4) for c in ctxv], "kal": [round(float(k), 4) for k in kal if k is not None], "kfine": [round(float(k), 4) for k in kfine if k is not None], "v1": round(v1, 7)}
            store.db.execute("INSERT OR IGNORE INTO curve_vectors (ticker, minute, series, close_ts, vec, bid, ask, y, hit_yes, hit_no) VALUES (?,?,?,?,?,?,?,?,?,?)",
                             (w["ticker"], m, w["series"], close, json.dumps(vec), bid, ask, y, hit_yes, hit_no))
            added += 1
    store.db.commit()
    log.info("vectors: %d points saved", added)
    return added
