"""Which size of "typical move" gives the cushion read that best separates the 15-minute crypto markets' winners from losers? (2026-11-24; user, on the DOGE 08:15 YES bet: "a typical minute is over 60s which isn't accurate in these 15m markets. things can change in 10s"; "what are we doing to compare typical minute, with typical 20s, or typical 15 min, or typical hour?")
QUESTION (one): read the cushion as (gap to Kalshi's own strike) / (unit x sqrt(time left in that unit)), with the unit = a typical 10 s, 20 s, 60 s (the minute the checks use: the one-second move x sqrt(60)), 15 minute or 60 minute move of that coin's settlement index, and the gap taken two ways (the instant gap: the last 10 s of ticks; the blend: the mean of the 1, 3 and 5 minute averages, as binaryrisk.readings blends it). For each of the ten reads, does the read sort the candidate buys by how often the side settled right, beyond what the ask already says?
CANDIDATES (the same set for every read): every settled crypto 15-minute market since the first full day of the tape (11-10), at 180 s and at 360 s after the open (the early window the lane buys in), on each side whose ask was 0.55-0.85 (the lane's price range, so no ask of 0.45-0.55) and whose quote was under 30 s old; held to settlement, a take at the ask, fee 0.07 p (1-p). The strike is the market's own (windows.strike), NOT the index at the open: the two differ (printed below), and priskp_run.py (the earlier thin-cushion and ridge studies) used the index at the open.
UNITS (rms of log moves of the settlement index; the sizes in the unit's own seconds, scaled to the time left by the square root of time, as the check does): 10 s, 20 s and 60 s over the last 15 minutes of ticks; 900 s over the last 24 hours (overlapping moves each 60 s); 3600 s over the last 24 hours. A unit needs its full history, so the first day of the tape is left out.
READ: the bets are cut into fifths of each read by this sample's own cuts (said so; no setting is chosen). Per read: won share and ask paid in the bottom and the top fifth, the gap between them (won share less ask) top less bottom, with its standard error from the fifths' sample sizes (markets repeat at 180 s and 360 s, so the true error is somewhat larger), and the net per $1 staked after the fee in the top fifth. A read counts only with its error in view. Read-only; a lead only.
Run: SPOT=0 scripts/cloud_job.sh scripts/priskp_cushion_units_study.py   (ONLY=KXDOGE15M-26NOV240815-15 on the host for one market: scripts/cloud.sh run scripts/priskp_cushion_units_study.py ONLY=...)"""
import bisect
import math
import os
import sys

from kalshi_moonshot import config, db
from kalshi_moonshot.kalshiws import index_for_series

s = config.load()
st = db.connect(s.db_path, "main", shared=True)
tape = db.connect(s.tape_path, "tape", shared=True)
SERIES = ("KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M", "KXDOGE15M", "KXADA15M", "KXBCH15M", "KXBNB15M", "KXHYPE15M", "KXNEAR15M", "KXZEC15M")
SAMPLE_S = (180.0, 360.0)
UNITS = (("10s", 10), ("20s", 20), ("60s", 60), ("15m", 900), ("60m", 3600))
ONLY = os.environ.get("ONLY")


def fee(p):
    return 0.07 * p * (1.0 - p)


def build_grid(T, V):
    t0, t1 = int(T[0]) + 1, int(T[-1])
    g = []
    k = 0
    for sec in range(t0, t1):
        while k + 1 < len(T) and T[k + 1] <= sec:
            k += 1
        g.append(V[k] if sec - T[k] <= 5.0 else 0.0)
    return t0, g


def lag_prefix(g, L):
    """cumulative sum of squared log moves over L seconds ending at each second (0 where either end is missing)"""
    out = [0.0] * len(g)
    acc = 0.0
    cnt = [0] * len(g)
    c = 0
    for i in range(len(g)):
        if i >= L and g[i] > 0 and g[i - L] > 0:
            d = math.log(g[i] / g[i - L])
            acc += d * d
            c += 1
        out[i] = acc
        cnt[i] = c
    return out, cnt


def unit_size(pref, cnt, i, window, L):
    """rms log move over L seconds, from the moves ending in the last `window` seconds before index i, scaled to the unit's own L"""
    a = max(0, i - window)
    n = cnt[i] - cnt[a]
    if n < (600 if window <= 900 else 20000):
        return None
    return math.sqrt((pref[i] - pref[a]) / n)


def reads_at(g, t0, prefs, strike, now, close):
    """{(unit, gap kind): cushion read for YES} at `now`"""
    i = int(now - t0)
    if i < 3700 or i >= len(g) or g[i] <= 0:
        return None
    left = close - now
    a10 = [x for x in g[i - 9 : i + 1] if x > 0]
    inst = math.log((sum(a10) / len(a10)) / strike)

    def avg(m):
        a = [x for x in g[max(0, i - 60 * m + 1) : i + 1] if x > 0]
        return sum(a) / len(a)

    blend = math.log(((avg(1) + avg(3) + avg(5)) / 3.0) / strike)
    out = {}
    for name, L in UNITS:
        pref, cnt = prefs[L if L in prefs else 1]
        if name == "60s":  # the check's own minute: the one-second move x sqrt(60), over the last 15 minutes
            p1, c1 = prefs[1]
            u1 = unit_size(p1, c1, i, 900, 1)
            if u1 is None:
                return None
            u, per = u1 * math.sqrt(60.0), 60.0
        else:
            window = 900 if L <= 60 else 86400
            u = unit_size(pref, cnt, i, window, L)
            if u is None:
                return None
            per = float(L)
        if u <= 0:
            return None
        div = u * math.sqrt(max(left, 1.0) / per)
        out[(name, "instant")] = inst / div
        out[(name, "blend")] = blend / div
    return out


rows = []
strike_gap = []
for ser in SERIES:
    ix = index_for_series(ser)
    ws = st.execute("SELECT ticker, close_ts, strike, result FROM windows WHERE series=? AND result IN ('yes','no') AND strike>0 ORDER BY close_ts", (ser,)).fetchall()
    if ONLY:
        ws = [w for w in ws if w["ticker"] == ONLY]
        if not ws:
            continue
    if not ws:
        continue
    first = float(ws[0]["close_ts"])
    lo, hi = first - 900.0 - 90000.0, float(ws[-1]["close_ts"]) + 5.0
    tk = tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND ts BETWEEN ? AND ? ORDER BY ts", (ix, lo, hi)).fetchall()
    if len(tk) < 100000:
        continue
    T, V = [float(r[0]) for r in tk], [float(r[1]) for r in tk]
    del tk
    t0, g = build_grid(T, V)
    prefs = {L: lag_prefix(g, L) for L in (1, 10, 20, 900, 3600)}
    for w in ws:
        close, strike, res = float(w["close_ts"]), float(w["strike"]), w["result"]
        opened = close - 900.0
        k0 = bisect.bisect_left(T, opened)
        if k0 < len(T) and T[k0] - opened <= 25.0:
            strike_gap.append(math.log(V[k0] / strike) * 1e4)
        qs = None
        for sec in SAMPLE_S:
            now = opened + sec
            r = reads_at(g, t0, prefs, strike, now, close)
            if r is None:
                continue
            if qs is None:
                qs = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (w["ticker"], opened - 60.0, close)).fetchall()
                if len(qs) < 20:
                    break
                qt = [float(x[0]) for x in qs]
                sc = 100.0 if max(float(x[2]) for x in qs) > 1.5 else 1.0
            j = bisect.bisect_right(qt, now) - 1
            if j < 0 or now - qt[j] > 30.0:
                continue
            yb, ya = float(qs[j][1]) / sc, float(qs[j][2]) / sc
            for side, ask in (("yes", ya), ("no", 1.0 - yb)):
                if ONLY:
                    print(f"{w['ticker']} {side} at {int(sec)} s: ask {ask:.2f}, strike {strike}", {f"{a}/{b}": round((v if side == 'yes' else -v), 2) for (a, b), v in r.items()})
                if not (0.55 <= ask <= 0.85):
                    continue
                won = res == side
                rows.append({"ser": ser, "t": sec, "ask": ask, "won": won, "net": (1.0 if won else 0.0) - ask - fee(ask), "reads": {kk: (v if side == "yes" else -v) for kk, v in r.items()}})
    print(ser, "done", len(rows), "candidates so far", file=sys.stderr)
if ONLY:
    sys.exit(0)
sg = sorted(strike_gap)
print(f"strike against the index at the open (bp, log): n {len(sg)}, median {sg[len(sg) // 2]:+.2f}, 10th {sg[len(sg) // 10]:+.2f}, 90th {sg[9 * len(sg) // 10]:+.2f}, mean abs {sum(abs(x) for x in sg) / len(sg):.2f}")
print(f"{len(rows)} candidate buys (ask 0.55-0.85, 180 s and 360 s after the open), {len({(r['ser']) for r in rows})} series; won {100 * sum(r['won'] for r in rows) / len(rows):.1f}%, ask {sum(r['ask'] for r in rows) / len(rows):.3f}")


def stat(g):
    n = len(g)
    w = sum(r["won"] for r in g) / n
    a = sum(r["ask"] for r in g) / n
    per = [r["net"] / (r["ask"] + fee(r["ask"])) for r in g]
    m = sum(per) / n
    return n, w, a, m, math.sqrt(sum((x - m) ** 2 for x in per) / (n - 1) / n)


print(f"{'read':14s} {'bottom fifth: n won ask gap':32s} {'top fifth: n won ask gap':32s} {'top-bottom gap':>16s} {'top net/$1':>14s}")
for name, _ in UNITS:
    for kind in ("instant", "blend"):
        key = (name, kind)
        ordered = sorted(rows, key=lambda r: r["reads"][key])
        f = len(ordered) // 5
        lo, hi = ordered[:f], ordered[-f:]
        a, b = stat(lo), stat(hi)
        gl, gh = a[1] - a[2], b[1] - b[2]
        se = math.sqrt(a[4] ** 2 + b[4] ** 2)  # the per-$ error of the nets, a stand-in for the gaps' error (the same bets, scaled by the ask)
        print(f"{name + '/' + kind:14s} {a[0]:5d} {100 * a[1]:5.1f}% {a[2]:.3f} {100 * gl:+6.1f}c      {b[0]:5d} {100 * b[1]:5.1f}% {b[2]:.3f} {100 * gh:+6.1f}c     {100 * (gh - gl):+7.1f}c (+/-{100 * se:.1f}) {b[3]:+8.3f} +/-{b[4]:.3f}")
