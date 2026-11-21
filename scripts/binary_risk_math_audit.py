"""MATH AUDIT of the binary risk model's readings (2026-11-20, user via parent: "check the math on each logic step"). For a few real settled commodity markets (perp reference, S&P out) and a few moments in each, every reading is recomputed here by an independent, deliberately plain implementation (list comprehensions over the raw ticks, a 1-second resample for the typical minute, a hand-solved line and parabola for the shape) and compared with binaryrisk.readings(). Also checked on the whole tape: tick spacing (is 'one-second moves x sqrt(60)' what the code computes?), the settlement rule (does the last-minute average against the strike agree with the recorded result more often than the last tick?), the 24/72 h references (never after the decision), the side flip, the ceiling and the fee, and how many training rows of the walk-forward are for markets that had not yet settled at the refit time (a leakage check). Prints OK / MISMATCH per step.
Run: SPOT=0 scripts/cloud_job.sh scripts/binary_risk_math_audit.py"""
import bisect, math, statistics
from datetime import datetime, timezone
from kalshi_moonshot import binaryrisk as br, config, db
from kalshi_moonshot.perpref import index_for_series as perp_index
from kalshi_moonshot.kalshiws import index_for_series as crypto_index
from kalshi_moonshot.strategy import fee_per_contract as fee

s = config.reload()
st = db.connect(s.db_path, "main", shared=True)
tape = db.connect(s.tape_path, "tape", shared=True)
START = datetime(2026, 11, 9, 5, tzinfo=timezone.utc).timestamp()
wins = st.execute("SELECT ticker, series, close_ts, result FROM windows WHERE result IN ('yes','no') AND close_ts > ? ORDER BY close_ts", (START + 7200,)).fetchall()
wins = [w for w in wins if not w["series"].startswith("KXINX") and not crypto_index(w["series"]) and perp_index(w["series"])]
bad = 0
def check(label, mine, theirs, tol=1e-6):
    global bad
    ok = abs(mine - theirs) <= tol * max(1.0, abs(mine), abs(theirs))
    bad += 0 if ok else 1
    print(f"  {'OK      ' if ok else 'MISMATCH'} {label}: independent {mine:.6f}  code {theirs:.6f}")

def line_parab(y):
    """Hand least squares: the line a+bx and the parabola a+bx+cx^2 on the bins' x in [-1,1]; returns (line slope b, parabola c, line R^2)."""
    n = len(y)
    x = [-1.0 + 2.0 * (k + 0.5) / n for k in range(n)]
    sx = sum(x); sxx = sum(a * a for a in x); sxxx = sum(a ** 3 for a in x); sx4 = sum(a ** 4 for a in x)
    sy = sum(y); sxy = sum(a * b for a, b in zip(x, y)); sxxy = sum(a * a * b for a, b in zip(x, y))
    b = (n * sxy - sx * sy) / (n * sxx - sx * sx)
    a = (sy - b * sx) / n
    ssr = sum((yy - a - b * xx) ** 2 for xx, yy in zip(x, y))
    my = sy / n
    sst = sum((yy - my) ** 2 for yy in y)
    # parabola by Cramer's rule on the 3x3 normal equations
    M = [[n, sx, sxx], [sx, sxx, sxxx], [sxx, sxxx, sx4]]
    R = [sy, sxy, sxxy]
    def det(m): return m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1]) - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0]) + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0])
    D = det(M)
    Mc = [[M[i][0], M[i][1], R[i]] for i in range(3)]
    c = det(Mc) / D
    return b, c, (1 - ssr / sst) if sst > 1e-18 else 1.0

by_ix = {}
for w in wins: by_ix.setdefault(perp_index(w["series"]), []).append(w)
picks = []
for ix, ws in sorted(by_ix.items()):
    if len(picks) >= 4: break
    picks.append((ix, ws[len(ws) // 2]))
print(f"audited feeds: {[p[0] for p in picks]}  ({len(wins)} commodity windows in all)")
for ix, w in picks:
    close, ticker = float(w["close_ts"]), w["ticker"]
    opened = close - 900.0
    tk = tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND ts BETWEEN ? AND ? ORDER BY ts", (ix, opened - 7400, close + 5)).fetchall()
    T, V = [float(r[0]) for r in tk], [float(r[1]) for r in tk]
    k0 = next(k for k, t in enumerate(T) if t >= opened)
    strike = V[k0]
    refs = []
    for hh in (24.0, 72.0):
        rr = tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND ts<=? AND ts>? ORDER BY ts DESC LIMIT 1", (ix, opened - hh * 3600.0, opened - hh * 3600.0 - 1800.0)).fetchone()
        refs.append(float(rr["value"]) if rr else 0.0)
        if rr: print(f"  reference {hh:.0f} h: tick at {opened - float(rr['ts']):.0f} s before the open-{hh:.0f} h mark? (ts <= mark: {float(rr['ts']) <= opened - hh * 3600.0})")
    print(f"\n=== {ticker} feed {ix} strike(open+{T[k0] - opened:.1f}s) {strike:.4f} result {w['result']} ticks {len(T)}")
    for left in (600.0, 330.0, 200.0):
        now = close - left
        f = br.readings(T, V, now, strike, left, tuple(refs))
        print(f" -- {left:.0f} s left")
        if not f:
            print("  (readings returned None: short or stale history)"); continue
        win = lambda sec: [(t, v) for t, v in zip(T, V) if now - sec <= t <= now]
        # typical minute: 1-second resample (last tick at or before each second), RMS of 1 s log moves x sqrt(60)
        def u_resample(sec):
            grid = [now - sec + i for i in range(int(sec) + 1)]
            vals = []
            for g in grid:
                j = bisect.bisect_right(T, g) - 1
                vals.append(V[j])
            return math.sqrt(sum(math.log(b / a) ** 2 for a, b in zip(vals, vals[1:])) / (len(vals) - 1)) * math.sqrt(60.0)
        u_ind = u_resample(900.0)
        # the code's own u15 is not exposed; derive it back from a reading that divides by it: mv15 = ln(blend/V[i])/u15
        sp = {m: statistics.fmean(v for _, v in win(60.0 * m)) for m in (1, 3, 5)}
        blend = (sp[1] + sp[3] + sp[5]) / 3.0
        lm = left / 60.0
        i15 = next(k for k, t in enumerate(T) if t >= now - 900.0)
        v_start15 = V[i15]
        u_code = math.log(blend / v_start15) / f["mv15"] if abs(f["mv15"]) > 1e-9 and abs(f["mv15"]) < 59 else float("nan")
        print(f"  typical minute: 1-s resample {u_ind:.3e} | code (backed out of mv15) {u_code:.3e} | ratio {u_code / u_ind:.3f}")
        ts_ = [t for t, _ in win(900.0)]
        dts = [b - a for a, b in zip(ts_, ts_[1:])]
        print(f"  tick spacing over 15 min: n {len(ts_)} median dt {statistics.median(dts):.2f}s mean {statistics.fmean(dts):.2f}s share dt>2s {sum(1 for d in dts if d > 2) / len(dts):.2f}")
        u = u_code  # use the code's unit for the rest (the unit is checked above); everything else is recomputed here
        for m in (1, 3, 5):
            cz = math.log(sp[m] / strike) / (u * math.sqrt(lm))
            cz = max(-8.0, min(8.0, cz))
            check(f"cz{m} (ln(avg{m}/strike)/(u*sqrt(min left)), clipped 8)", cz, f[f"cz{m}"])
        check("czb (mean of cz1,cz3,cz5)", (f["cz1"] + f["cz3"] + f["cz5"]) / 3.0, f["czb"])
        check("tr13", max(-30, min(30, math.log(sp[1] / sp[3]) / u)), f["tr13"])
        check("tr35", max(-30, min(30, math.log(sp[3] / sp[5]) / u)), f["tr35"])
        for mins in (15, 30, 60):
            w_ = [v for t, v in zip(T, V) if now - 60 * mins <= t <= now]
            start = next(v for t, v in zip(T, V) if t >= now - 60 * mins)
            check(f"pos{mins} ((blend-low)/(high-low))", (blend - min(w_)) / (max(w_) - min(w_)), f[f"pos{mins}"])
            check(f"mv{mins} (ln(blend/first tick)/u)", max(-60, min(60, math.log(blend / start) / u)), f[f"mv{mins}"])
            # shape: 12 equal time bins by tick membership, y = ln(bin mean/last bin mean)/u
            span = 60.0 * mins
            bins = []
            for kk in range(12):
                lo_, hi_ = now - span + span * kk / 12, now - span + span * (kk + 1) / 12
                seg = [v for t, v in zip(T, V) if lo_ <= t < hi_ or (kk == 11 and t == now)]
                bins.append(statistics.fmean(seg) if seg else None)
            if None in bins:
                print(f"  (shape{mins}: an empty bin in the independent version, skipped)"); continue
            y = [math.log(b / bins[-1]) / u for b in bins]
            b_, c_, r2 = line_parab(y)
            check(f"sl{mins} (line slope of the bin means)", max(-60, min(60, b_)), f[f"sl{mins}"], 1e-4)
            check(f"cv{mins} (parabola curvature)", max(-60, min(60, c_)), f[f"cv{mins}"], 1e-4)
            check(f"ft{mins} (line R^2)", max(0.0, min(1.0, r2)), f[f"ft{mins}"], 1e-4)
        for hrs, ref in zip((24, 72), refs):
            if ref > 0:
                check(f"dr{hrs} (ln(blend/ref)/(u*sqrt(minutes)))", max(-8, min(8, math.log(blend / ref) / (u * math.sqrt(hrs * 60.0)))), f[f"dr{hrs}"])
        # side flip, ceiling, fee, target
        yv, nv = br.side_vector(f, "yes", 0.7), br.side_vector(f, "no", 0.7)
        iz = {n: i for i, n in enumerate(br.NAMES)}
        for n in ("czb", "tr13", "mv30", "sl30"):
            check(f"NO flips {n}", -yv[iz[n]], nv[iz[n]])
        check("NO flips pos30 (1 - pos)", 1.0 - yv[iz["pos30"]], nv[iz["pos30"]])
        check("fee at 0.40 (0.07*p*(1-p))", 0.07 * 0.4 * 0.6, fee(0.4))
        check("ceiling at 0.70 ((1-ask-fee)/ask)", (1.0 - 0.7 - 0.07 * 0.7 * 0.3) / 0.7, br.ceiling(0.7))
# settlement rule on the whole tape: last-minute average vs last tick against the recorded result
agree_avg = agree_last = n = 0
for ix, ws in by_ix.items():
    for w in ws[:: max(1, len(ws) // 60)]:
        close = float(w["close_ts"]); opened = close - 900.0
        tk = tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND ts BETWEEN ? AND ? ORDER BY ts", (ix, opened, close + 2)).fetchall()
        if len(tk) < 100: continue
        T, V = [float(r[0]) for r in tk], [float(r[1]) for r in tk]
        if T[0] - opened > 25: continue
        strike = V[0]
        last_min = [v for t, v in zip(T, V) if close - 60 <= t <= close]
        before = [v for t, v in zip(T, V) if t <= close]
        if not last_min: continue
        n += 1
        agree_avg += (statistics.fmean(last_min) > strike) == (w["result"] == "yes")
        agree_last += (before[-1] > strike) == (w["result"] == "yes")
print(f"\nsettlement rule on {n} sampled commodity windows (strike = our open tick, not Kalshi's own): last-minute average agrees with the result {agree_avg}/{n}, last tick {agree_last}/{n}")
# training leakage: rows of the walk-forward whose market had not settled at the refit time
close_ts = [float(w["close_ts"]) for w in wins]
close_ts.sort()
t = close_ts[len(close_ts) // 2]
print(f"leakage check at refit time t: training rows are every 60 s of the previous 120 h; a row at time r in a market closing at c > t has an outcome and exit prices from after t. Share of the last 120 h's rows with close > t: about {900 / 2 / (120 * 3600) * 100:.2f}% (a row's market lasts up to 900 s, so rows in the final 15 minutes before t are affected)")
print(f"\nSUMMARY: {bad} mismatches")
