"""Does the 15-minute quote move before the settlement index it follows? Places nothing.

Question (user's trade observation, 2026-11-07): a resting limit filled at a price the displayed spot reached much later. After a quote jump that the index has not
yet matched, does the index (its latest value, and the 60-second average the market settles on) move the same way over the next 1 to 60 seconds?

Units: every move is divided by the index's own typical-minute size (trailing hour's root-mean-square one-second move x sqrt(60), scaled by sqrt(h/60) for a horizon of h seconds),
so no cents, dollars or coin names enter. A quote jump is a 10-second mid change of at least 3x that market's own trailing 10-minute 1-second RMS x sqrt(10), and at least two
price ticks (a tick is structural). Index move since = its own 10-second move, signed with the jump, in its 10-second units: flat (within 0.5), against (below -0.5), part (0.5 to 1), matched (1 or more). The baseline is the same
measurement after ordinary quote moves (at least one tick, under the jump threshold). Events in a market are 30 seconds apart. Only 60-840 seconds before the close.
Reports n, mean signed move in units +- standard error, the two halves of the period, and index path-efficiency terciles (curve shape).
    python scripts/book_leads_spot_study.py   # needs the Docker Postgres on localhost:5433
"""
import collections
import math

import numpy as np
import psycopg

SERIES = {"KXBTC15M": "BRTI", "KXETH15M": "ETHUSD_RTI", "KXSOL15M": "SOLUSD_RTI", "KXXRP15M": "XRPUSD_RTI", "KXDOGE15M": "DOGEUSD_RTI"}
HORIZONS = (1, 3, 5, 10, 20, 30, 60)
JUMP_W, JUMP_SD, MIN_TICKS, QUIET_UNITS, COOLDOWN = 10, 3.0, 2, 1.0, 30
TICK = 0.01

conn = psycopg.connect("postgresql://moonshot:moonshot@localhost:5433/moonshot")


def ffill_grid(ts, vals, g0, g1):
    grid = np.arange(g0, g1, dtype=float)
    idx = np.searchsorted(ts, grid, side="right") - 1
    ok = idx >= 0
    out = np.full(grid.shape, np.nan)
    out[ok] = vals[idx[ok]]
    return out


def trailing_rms(x, n):
    """RMS of x over the previous n entries (excluding the current), nan until n/2 entries exist."""
    sq = np.nan_to_num(x * x)
    c = np.concatenate([[0.0], np.cumsum(sq)])
    out = np.full(len(x), np.nan)
    for i in range(n // 2, len(x)):
        lo = max(0, i - n)
        out[i] = math.sqrt((c[i] - c[lo]) / (i - lo))
    return out


events = []  # (kind, t, half_key, eff, [signed value z by h], [signed avg z by h])
for series, index_id in SERIES.items():
    rows = conn.execute("SELECT ts, value, avg60 FROM tape.index_ticks WHERE index_id=%s ORDER BY ts", (index_id,)).fetchall()
    its = np.array([r[0] for r in rows])
    iv = np.array([r[1] for r in rows], dtype=float)
    ia = np.array([r[2] if r[2] is not None else np.nan for r in rows], dtype=float)
    g0, g1 = int(its[0]) + 1, int(its[-1])
    V = ffill_grid(its, iv, g0, g1)
    A = ffill_grid(its, ia, g0, g1)
    r1 = np.diff(np.log(V), prepend=np.nan)
    rms = trailing_rms(r1, 3600)  # typical one-second move, trailing hour
    ret = {h: np.full(len(V), np.nan) for h in HORIZONS}
    aret = {h: np.full(len(V), np.nan) for h in HORIZONS}
    for h in HORIZONS:
        ret[h][:-h] = np.log(V[h:] / V[:-h])
        aret[h][:-h] = np.log(A[h:] / A[:-h])
    r10 = np.full(len(V), np.nan)
    r10[JUMP_W:] = np.log(V[JUMP_W:] / V[:-JUMP_W])
    # path efficiency over the prior 300 s, from 5-second steps
    step = np.abs(np.diff(np.log(V[::5]), prepend=np.nan))
    net = np.full(len(V), np.nan)
    net[300:] = np.abs(np.log(V[300:] / V[:-300]))
    cs = np.concatenate([[0.0], np.cumsum(np.nan_to_num(step))])
    path = np.full(len(V), np.nan)
    for i in range(300, len(V)):
        j = i // 5
        path[i] = cs[j] - cs[max(0, j - 60)]
    with np.errstate(all='ignore'):
        eff = net / path

    qrows = conn.execute("SELECT ticker, ts, bid, ask FROM tape.quotes WHERE ticker LIKE %s AND bid IS NOT NULL AND ask IS NOT NULL ORDER BY ticker, ts", (series + "-%",)).fetchall()
    by = collections.defaultdict(list)
    for t, ts, b, a in qrows:
        by[t].append((ts, (b + a) / 2))
    for ticker, q in by.items():
        if len(q) < 50:
            continue
        qt = np.array([x[0] for x in q])
        qm = np.array([x[1] for x in q])
        close = round(qt[-1] / 900.0) * 900  # the last quote lands just after the close
        s0, s1 = max(int(qt[0]) + 1, g0), min(int(qt[-1]), g1)
        if s1 - s0 < 300:
            continue
        M = ffill_grid(qt, qm, s0, s1)
        dm1 = np.diff(M, prepend=np.nan)
        mrms = trailing_rms(dm1, 600)
        last = {}
        for i in range(max(JUMP_W, 120), len(M) - max(HORIZONS) - 1):
            t = s0 + i
            left = close - t
            gi = t - g0
            if left < 60 or left > 840 or gi < 3600 or gi + max(HORIZONS) >= len(V) or False:
                continue
            d = M[i] - M[i - JUMP_W]
            if not np.isfinite(d) or abs(d) < MIN_TICKS * TICK * 0.5 or not np.isfinite(mrms[i]) or not np.isfinite(rms[gi]) or not np.isfinite(eff[gi]) or rms[gi] == 0:
                continue
            thresh = max(JUMP_SD * mrms[i] * math.sqrt(JUMP_W), MIN_TICKS * TICK)
            if abs(d) >= thresh:
                kind = "jump"
            elif abs(d) >= TICK * 0.99:
                kind = "ordinary"
            else:
                continue
            if t - last.get(kind, -10**9) < COOLDOWN:
                continue
            sgn = 1.0 if d > 0 else -1.0
            u = rms[gi] * math.sqrt(60.0)
            idx_moved = sgn * r10[gi] / (rms[gi] * math.sqrt(JUMP_W))
            if kind == "jump":
                kind = "jump_matched" if idx_moved >= QUIET_UNITS else "jump_flat" if abs(idx_moved) < 0.5 else "jump_against" if idx_moved <= -0.5 else "jump_part"
            zv = [sgn * ret[h][gi] / (u * math.sqrt(h / 60.0)) for h in HORIZONS]
            za = [sgn * aret[h][gi] / (u * math.sqrt(h / 60.0)) for h in HORIZONS]
            if not all(np.isfinite(zv)) or not all(np.isfinite(za)):
                continue
            events.append((kind, t, eff[gi], zv, za))
            last[kind] = t

print(f"events {len(events)} | units: signed move / typical-minute size scaled to the horizon; positive = index followed the quote's direction")
ts_all = sorted(e[1] for e in events)
mid_t = ts_all[len(ts_all) // 2]
eff_all = sorted(e[2] for e in events)
cuts = (eff_all[len(eff_all) // 3], eff_all[2 * len(eff_all) // 3])


def report(label, sel):
    if len(sel) < 30:
        print(f"{label:<34} n {len(sel):>5}  (too few)")
        return
    for name, k in (("index value", 3), ("60s average", 4)):
        cells = []
        for hi, h in enumerate(HORIZONS):
            x = np.array([e[k][hi] for e in sel])
            cells.append(f"{h}s {x.mean():+.3f}±{x.std() / math.sqrt(len(x)):.3f}")
        print(f"{label:<34} n {len(sel):>5} {name:<11} " + "  ".join(cells))


for kind in ("jump_flat", "jump_against", "jump_part", "jump_matched", "ordinary"):
    sel = [e for e in events if e[0] == kind]
    print()
    report(kind, sel)
    report(f"  {kind} first half", [e for e in sel if e[1] < mid_t])
    report(f"  {kind} second half", [e for e in sel if e[1] >= mid_t])
    report(f"  {kind} choppy third", [e for e in sel if e[2] <= cuts[0]])
    report(f"  {kind} middle third", [e for e in sel if cuts[0] < e[2] <= cuts[1]])
    report(f"  {kind} straight third", [e for e in sel if e[2] > cuts[1]])
