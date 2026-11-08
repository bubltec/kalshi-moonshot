"""Does the 15-minute quote move before the settlement index it follows? Places nothing.

Question (user's trade observation, 2026-11-07): a resting limit filled at a price the displayed spot reached much later. After a quote jump that the index has not
yet matched, does the index (its latest value, and the 60-second average the market settles on) move the same way over the next 1 to 60 seconds?

Units: every move is divided by the index's own typical-minute size (trailing hour's root-mean-square one-second move x sqrt(60), scaled by sqrt(h/60) for a horizon of h seconds),
so no cents, dollars or coin names enter. A quote jump is a 10-second mid change of at least 3x that market's own trailing 10-minute 1-second RMS x sqrt(10), and at least two
price ticks (a tick is structural). Index move since = its own 10-second move, signed with the jump, in its 10-second units: flat (within 0.5), against (below -0.5), part (0.5 to 1), matched (1 or more). The baseline is the same
measurement after ordinary quote moves (at least one tick, under the jump threshold). Events in a market are 30 seconds apart. Only 60-840 seconds before the close.
Reports n, mean signed move in units +- standard error, the two halves of the period, and index path-efficiency terciles (curve shape).
    scripts/cloud.sh run scripts/book_leads_spot_study.py          # the cloud tape (pure Python: the bot image has no numpy)
    .venv/bin/python scripts/book_leads_spot_study.py              # this Mac's Docker Postgres on localhost:5433
"""
import bisect
import collections
import math
import os

SERIES = {"KXBTC15M": "BRTI", "KXETH15M": "ETHUSD_RTI", "KXSOL15M": "SOLUSD_RTI", "KXXRP15M": "XRPUSD_RTI", "KXDOGE15M": "DOGEUSD_RTI"}
HORIZONS = (1, 3, 5, 10, 20, 30, 60)
JUMP_W, JUMP_SD, MIN_TICKS, QUIET_UNITS, COOLDOWN = 10, 3.0, 2, 1.0, 30
TICK = 0.01
NAN = float("nan")
fin = math.isfinite

try:
    from kalshi_moonshot import config, db

    if not db.use_postgres():  # the bot's config falls back to the stale SQLite file on this Mac
        raise RuntimeError("not on the Postgres tape")
    _s = config.reload()
    _tape = db.connect(_s.tape_path, "tape", shared=True)

    def query(sql, params=()):
        return _tape.execute(sql.replace("%s", "?"), params).fetchall()
except Exception:  # not inside the bot image: use this Mac's Docker Postgres
    import psycopg

    _conn = psycopg.connect("postgresql://moonshot:moonshot@localhost:5433/moonshot")

    def query(sql, params=()):
        return _conn.execute(sql.replace(" FROM ", " FROM tape.", 1), params).fetchall()


def ffill_grid(ts, vals, g0, g1):
    out = []
    for t in range(g0, g1):
        i = bisect.bisect_right(ts, t) - 1
        out.append(vals[i] if i >= 0 else NAN)
    return out


def trailing_rms(x, n):
    """RMS of x over the previous n entries (excluding the current), nan until n/2 entries exist."""
    c = [0.0]
    for v in x:
        c.append(c[-1] + (v * v if fin(v) else 0.0))
    out = [NAN] * len(x)
    for i in range(n // 2, len(x)):
        lo = max(0, i - n)
        out[i] = math.sqrt((c[i] - c[lo]) / (i - lo))
    return out


def dlog(a, b):
    return math.log(b / a)


events = []  # (kind, t, path efficiency, [signed index-value move by horizon], [signed 60s-average move by horizon])
for series, index_id in SERIES.items():
    rows = query("SELECT ts, value, avg60 FROM index_ticks WHERE index_id=%s ORDER BY ts", (index_id,))
    its = [r[0] for r in rows]
    iv = [float(r[1]) if r[1] is not None else NAN for r in rows]
    ia = [float(r[2]) if r[2] is not None else NAN for r in rows]
    del rows
    g0, g1 = int(its[0]) + 1, int(its[-1])
    V = ffill_grid(its, iv, g0, g1)
    A = ffill_grid(its, ia, g0, g1)
    n = len(V)
    r1 = [NAN] + [dlog(V[i - 1], V[i]) for i in range(1, n)]
    rms = trailing_rms(r1, 3600)  # typical one-second move, trailing hour
    del r1
    # path efficiency over the prior 300 s, from 5-second steps
    sub = V[::5]
    cs = [0.0]
    for j in range(len(sub)):
        cs.append(cs[-1] + (abs(dlog(sub[j - 1], sub[j])) if j > 0 and fin(sub[j]) and fin(sub[j - 1]) else 0.0))

    def eff_at(gi):
        net = abs(dlog(V[gi - 300], V[gi]))
        j = gi // 5
        path = cs[j] - cs[max(0, j - 60)]
        return net / path if path > 0 else NAN

    qrows = query("SELECT ticker, ts, bid, ask FROM quotes WHERE ticker LIKE %s AND bid IS NOT NULL AND ask IS NOT NULL ORDER BY ticker, ts", (series + "-%",))
    by = collections.defaultdict(list)
    for t, ts, b, a in qrows:
        by[t].append((ts, (b + a) / 2))
    del qrows
    for ticker, q in by.items():
        if len(q) < 50:
            continue
        qt = [x[0] for x in q]
        qm = [x[1] for x in q]
        close = round(qt[-1] / 900.0) * 900  # the last quote lands just after the close
        s0, s1 = max(int(qt[0]) + 1, g0), min(int(qt[-1]), g1)
        if s1 - s0 < 300:
            continue
        M = ffill_grid(qt, qm, s0, s1)
        dm1 = [NAN] + [M[i] - M[i - 1] for i in range(1, len(M))]
        mrms = trailing_rms(dm1, 600)
        last = {}
        for i in range(max(JUMP_W, 120), len(M) - max(HORIZONS) - 1):
            t = s0 + i
            left = close - t
            gi = t - g0
            if left < 60 or left > 840 or gi < 3600 or gi + max(HORIZONS) >= n:
                continue
            d = M[i] - M[i - JUMP_W]
            if not fin(d) or abs(d) < MIN_TICKS * TICK * 0.5 or not fin(mrms[i]) or not fin(rms[gi]) or rms[gi] == 0:
                continue
            thresh = max(JUMP_SD * mrms[i] * math.sqrt(JUMP_W), MIN_TICKS * TICK)
            if abs(d) >= thresh:
                kind = "jump"
            elif abs(d) >= TICK * 0.99:
                kind = "ordinary"
            else:
                continue
            base = kind  # the cooldown runs on jump / ordinary, before a jump is split by what the index did
            if t - last.get(base, -10**9) < COOLDOWN:
                continue
            ef = eff_at(gi)
            if not fin(ef):
                continue
            sgn = 1.0 if d > 0 else -1.0
            u = rms[gi] * math.sqrt(60.0)
            idx_moved = sgn * dlog(V[gi - JUMP_W], V[gi]) / (rms[gi] * math.sqrt(JUMP_W))
            if kind == "jump":
                kind = "jump_matched" if idx_moved >= QUIET_UNITS else "jump_flat" if abs(idx_moved) < 0.5 else "jump_against" if idx_moved <= -0.5 else "jump_part"
            zv = [sgn * dlog(V[gi], V[gi + h]) / (u * math.sqrt(h / 60.0)) for h in HORIZONS]
            za = [sgn * dlog(A[gi], A[gi + h]) / (u * math.sqrt(h / 60.0)) for h in HORIZONS]
            if not all(fin(v) for v in zv) or not all(fin(v) for v in za):
                continue
            events.append((kind, t, ef, zv, za))
            last[base] = t
    print(f"{series} done, events so far {len(events)}", flush=True)

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
            x = [e[k][hi] for e in sel]
            m = sum(x) / len(x)
            sd = math.sqrt(sum((v - m) ** 2 for v in x) / len(x))
            cells.append(f"{h}s {m:+.3f}±{sd / math.sqrt(len(x)):.3f}")
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
