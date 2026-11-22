"""PRISKP, THE CRYPTO RUN, written fresh (2026-11-22; user via parent: "Stop copying other lanes this is a new lane. We need it fresh").
WHAT THE USER SAID, and nothing else is a rule: the 11 crypto 15-minute series on their Kalshi settlement indexes; buy window minutes 2-14 after the open ("like 2-14"); the checks are refusals (no buy on a NO or weak); held to settlement; never an ask of 0.45-0.55.
MY OWN CHOICES (named, shown as columns, not gates): the buy is a TAKE at the side's ask at the first 10-second reading in the window where every one of the five checks can be read and says YES (fifths 4-5; an unread check, any NO, WEAK or anomaly = no buy at that reading; changed 2026-11-22 after a buy on day one went in with every check unread); a quote at most 10 s old; one buy a market; fee 0.07 p (1-p); $10 a buy; markets settled since 2026-11-09 07:00 UTC with ticks and quotes; readings every 10 s; the strike is the index's own tick at the open; the checks and their own-history fifths as below. THE COMPARISON ("plain"): at the first reading in the window with a usable quote, the favourite side (the higher ask), no checks. "Refused" = markets where the plain buy exists and the model never buys.
THE CHECKS (side view, in the market's own typical minute u15 = the last 15 minutes' root-mean-square one-tick log move per minute): cushion (binaryrisk's blended 1/3/5-minute average against the strike, over u15 x sqrt(minutes left)), trend (1-minute against 3-minute average), history (share of the last 30 minutes on the side's winning side), price trend (the side's mid now against up to 3 minutes ago in the price's own typical minute) and price dip (the mid under its high of up to the last 5 minutes). Also read, not refusals: volatility ratio and every other price-free reading of binaryrisk.readings, and the wave shape (binaryrisk.shape: slope, curvature, line fit at 15/30/60 minutes), each cut into the series' own previous 24 hours in the same 3 minutes of the market (hourly cutoffs, 100 readings needed): fifths 1-5 for the checks and features, terciles 1-3 for the shape; anomaly = beyond the 1st/99th percentile.
IMPORTS ONLY: binaryrisk (readings, shape), config and db (the tape), kalshiws (the settlement index of a series), strategy (the fee). Nothing of any lane's order, band, exit or universe.
Writes priskp_buys_crypto.json.gz (every buy of the model and of the plain order, with readings, states, hour profile, chart data) and prints the tables.
Run: SPOT=0 scripts/cloud_job.sh scripts/priskp_run.py [LIMIT=markets per index, for a quick run]"""
import bisect, gzip, json, math, multiprocessing, os, time
from datetime import datetime, timezone
from pathlib import Path
from kalshi_moonshot import binaryrisk as br, config, db, priskp
from kalshi_moonshot.kalshiws import index_for_series as settlement_index
from kalshi_moonshot.strategy import fee_per_contract as fee

t0 = time.time()
START = datetime(2026, 11, 9, 7, tzinfo=timezone.utc).timestamp()
LIMIT = int(os.environ.get("LIMIT", "0"))
STEP, WIN_LO, WIN_HI, STALE, STAKE = 10, 2.0, 14.0, 10.0, 10.0
DEAD = (0.45, 0.55)  # the user's own: never an ask here
CHECKS = ("cushion", "trend", "history", "price", "pricedip")
FEAT_SIGNED = ("czb", "czlb", "tr13", "tr35", "mv15", "mv30", "mv60", "sl15", "sl30", "sl60", "cv15", "cv30", "cv60")
FEAT_POS = ("pos15", "pos30", "pos60")
FEAT_FREE = ("left", "vr", "amp15", "ft15", "ft30", "ft60")
FEATS = ["f:" + n for n in FEAT_SIGNED + FEAT_POS + FEAT_FREE]
SHAPES = ["t:" + a + str(w) for a in ("sl", "cv", "ft") for w in (15, 30, 60)]
NAMES = CHECKS + ("volatility",) + tuple(FEATS) + tuple(SHAPES)
QS = (0.01, 0.2, 0.4, 0.6, 0.8, 0.99, 1.0 / 3.0, 2.0 / 3.0)
cfg0 = config.reload()
st0 = db.connect(cfg0.db_path, "main", shared=True)
jobs = {}
for w in st0.execute("SELECT ticker, series, close_ts, result FROM windows WHERE result IN ('yes','no') AND close_ts > ? ORDER BY close_ts", (START,)).fetchall():
    ix = settlement_index(w["series"]) if w["series"].endswith("15M") else None
    if ix: jobs.setdefault(ix, []).append((w["ticker"], w["series"], float(w["close_ts"]), w["result"] == "yes"))
if LIMIT: jobs = {k: v[-LIMIT:] for k, v in jobs.items()}


def side_value(name, side, f, share, pt, dy, dn):
    """One named reading in the side's view (YES view -> NO view: direction readings flip, positions go 1-x)."""
    sg = 1.0 if side == "yes" else -1.0
    if name == "cushion": return sg * f["czb"]
    if name == "trend": return sg * f["tr13"]
    if name == "history": return share if side == "yes" else 1.0 - share
    if name == "price": return None if pt is None else sg * pt
    if name == "pricedip": return dy if side == "yes" else dn
    if name == "volatility": return f["vr"]
    key = name[2:]
    if key not in f: return None
    if name.startswith("t:"):
        return f[key] if key.startswith("ft") else sg * f[key]
    if key in FEAT_SIGNED: return sg * f[key]
    if key in FEAT_POS: return f[key] if side == "yes" else 1.0 - f[key]
    return f[key]


def build(job):
    ix, ws = job
    cfg = config.reload()
    tape = db.connect(cfg.tape_path, "tape", shared=True)
    tk = tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND ts BETWEEN ? AND ? ORDER BY ts", (ix, min(w[2] for w in ws) - 4900, max(w[2] for w in ws) + 5)).fetchall()
    T, V = [float(r[0]) for r in tk], [float(r[1]) for r in tk]
    del tk
    M = []
    for ticker, series, close, won_yes in ws:
        opened = close - 900.0
        k = bisect.bisect_left(T, opened)
        if k >= len(T) or T[k] - opened > 25.0: continue
        strike = V[k]
        q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (ticker, opened - 60.0, close)).fetchall()
        if len(q) < 20: continue
        qt = [float(r[0]) for r in q]
        sc = 100.0 if max(float(r[2]) for r in q) > 1.5 else 1.0
        qb, qa = [float(r[1]) / sc for r in q], [float(r[2]) / sc for r in q]
        # the YES mid every second from the open - 60 s (forward-filled), for the price curve
        mid = [None] * 961
        j = -1
        for s in range(961):
            while j + 1 < len(q) and qt[j + 1] <= opened - 60.0 + s: j += 1
            if j >= 0: mid[s] = (qb[j] + qa[j]) / 2.0
        first_mid = next((s for s in range(961) if mid[s] is not None), 961)
        i0, i1 = bisect.bisect_left(T, opened - 1900.0), bisect.bisect_right(T, close)
        cs = [0]
        for v in V[i0:i1]: cs.append(cs[-1] + (1 if v > strike else 0))
        R = []  # (seconds after the open, {name: (yes_value, no_value)}, quote ask/bid at that moment, quote age)
        t = opened + 60.0
        while t <= close - 20.0:
            s_open = t - opened
            f = br.readings(T, V, t, strike, close - t)
            jq = bisect.bisect_right(qt, t) - 1
            if f is None or jq < 0: t += STEP; continue
            je = bisect.bisect_right(T, t) - 1
            ia = max(i0, bisect.bisect_left(T, t - 1800.0))
            share = (cs[je + 1 - i0] - cs[ia - i0]) / max(1, je + 1 - ia)
            sidx = int(s_open) + 60
            L = min(300, sidx - first_mid)
            pt = dy = dn = None
            if L >= 60:
                g = [mid[sidx - 5 * x] for x in range(L // 5, -1, -1)]
                ch = [b - a for a, b in zip(g, g[1:])]
                pu = math.sqrt(sum(c * c for c in ch) / len(ch) * 12.0)
                if pu > 0:
                    back = min(180, L)
                    pt = (g[-1] - mid[sidx - back]) / (pu * math.sqrt(back / 60.0))
                    dy, dn = -(max(g) - g[-1]) / pu, -(g[-1] - min(g)) / pu
            vals = {side: {n: side_value(n, side, f, share, pt, dy, dn) for n in NAMES} for side in ("yes", "no")}
            sh = None
            if int(s_open) % 30 == 0:  # the confirmed shifts of the last hour, every 30 s (the entry study reads the latest at or before a buy)
                ux = priskp.typical_minute(T, V, t)
                got = priskp.shifts(T, V, t, ux, step_s=30.0) if ux > 0 else []
                sh = [(round(c["ago"]), round(c["step"] / max(ux, 1e-12), 2), round(c["held"], 2), round((c["before"] - strike) / max(ux, 1e-12), 2), round((c["after"] - strike) / max(ux, 1e-12), 2)) for c in got]  # the levels before and after against THIS market's strike, in typical minutes (YES view)
            R.append((s_open, vals, qa[jq], qb[jq], t - qt[jq], f, sh))
            t += STEP
        M.append({"ticker": ticker, "series": series, "close": close, "opened": opened, "won_yes": won_yes, "strike": strike, "R": R, "qt": qt, "qb": qb, "qa": qa})
    # each reading's own history: the series' previous 24 h, the same 3 minutes of the market, both sides
    H = {}
    for m in M:
        for s_open, vals, _a, _b, _age, _f, _sh in m["R"]:
            tt, bk = m["opened"] + s_open, min(4, int(s_open // 180))
            for side in ("yes", "no"):
                for n in NAMES:
                    x = vals[side][n]
                    if x is not None: H.setdefault((bk, n), []).append((tt, x))
    HT = {}
    for key, v in H.items():
        v.sort(); HT[key] = [x[0] for x in v]
    cuts = {}

    def state(tt, s_open, name, x):
        if x is None: return "unread"
        key, hour = (min(4, int(s_open // 180)), name), int(tt // 3600)
        c = cuts.get((key, hour), 0)
        if c == 0:
            ts = HT.get(key, [])
            own = sorted(y[1] for y in H.get(key, [])[bisect.bisect_left(ts, hour * 3600.0 - 86400.0):bisect.bisect_left(ts, hour * 3600.0)])
            c = cuts[(key, hour)] = None if len(own) < 100 else tuple(own[min(len(own) - 1, int(len(own) * p))] for p in QS)
        if c is None: return "unread"
        if x < c[0] or x > c[5]: return "anomaly"
        if name.startswith("t:"): return 1 if x <= c[6] else 2 if x <= c[7] else 3
        return 1 + sum(1 for z in c[1:5] if x > z)

    def hour_profile(m, side, s_open):
        t, strike, sg = m["opened"] + s_open, m["strike"], (1.0 if side == "yes" else -1.0)
        j = bisect.bisect_right(T, t) - 1
        i15, i60 = bisect.bisect_left(T, t - 900.0), bisect.bisect_left(T, t - 3600.0)
        if j - i60 < 1500 or j - i15 < 300: return None
        sq = sum(math.log(V[k] / V[k - 1]) ** 2 for k in range(i15 + 1, j + 1) if V[k] > 0 and V[k - 1] > 0)
        u = math.sqrt(sq / (T[j] - T[i15]) * 60.0) if T[j] > T[i15] and sq > 0 else None
        if not u: return None
        g = [V[max(bisect.bisect_right(T, t - 3600.0 + 10.0 * x) - 1, 0)] for x in range(361)]
        lv = lambda v: sg * math.log(v / strike) / u
        mv = sum(abs(math.log(b / a)) for a, b in zip(g, g[1:]))
        hi, lo = max(range(361), key=lambda x: sg * g[x]), min(range(361), key=lambda x: sg * g[x])
        return {"u15": u, "bins": [round(lv(sum(g[30 * b:30 * b + 30]) / 30.0), 3) for b in range(12)], "blocks": [round(sum(1 for v in g[60 * b:60 * b + 60] if sg * (v - strike) > 0) / 60.0, 3) for b in range(6)],
                "cross": sum(1 for a, b in zip(g, g[1:]) if (a - strike) * (b - strike) < 0), "hi_ago": round((360 - hi) / 6.0, 1), "lo_ago": round((360 - lo) / 6.0, 1), "range": round(math.log(max(g) / min(g)) / u, 3),
                "drift": round(sg * math.log(g[-1] / g[0]) / u, 3), "eff": round(abs(math.log(g[-1] / g[0])) / mv, 4) if mv > 0 else 0.0}

    def chart(m, side, s_open, price):
        idx = []
        for x in range(int((m["close"] - (m["opened"] - 3600.0)) // 10) + 1):
            a = bisect.bisect_right(T, m["opened"] - 3600.0 + 10.0 * x) - 1
            idx.append(None if a < 0 else round(100.0 * math.log(V[a] / m["strike"]), 4))
        px = []
        for x in range(91):
            jq = bisect.bisect_right(m["qt"], m["opened"] + 10.0 * x) - 1
            if jq < 0 or m["opened"] + 10.0 * x - m["qt"][jq] > 30.0: px.append(None); continue
            b, a = m["qb"][jq], m["qa"][jq]
            px.append([round(b, 3), round(a, 3)] if side == "yes" else [round(1.0 - a, 3), round(1.0 - b, 3)])
        return {"idx": idx, "px": px, "buy_s": s_open, "price": price}

    out, decisions = [], []
    for m in M:
        won = {"yes": m["won_yes"], "no": not m["won_yes"]}
        got = {}
        log = []  # every reading's decision in the window (2026-11-22, the entry study): (seconds after the open, YES ask, NO ask, the five checks' fifths for YES, for NO; A anomaly, U unread)
        code = lambda v: "A" if v == "anomaly" else "U" if v == "unread" else str(v)
        for s_open, vals, ya, yb, age, f, sh in m["R"]:
            if not (WIN_LO * 60.0 <= s_open <= WIN_HI * 60.0) or age > STALE: continue
            tt = m["opened"] + s_open
            ask = {"yes": ya, "no": 1.0 - yb}
            cs = {s: "".join(code(state(tt, s_open, c, vals[s][c])) for c in CHECKS) for s in ("yes", "no")}
            log.append((int(s_open), round(ask["yes"], 3), round(ask["no"], 3), cs["yes"], cs["no"], round(vals["yes"]["cushion"], 3), round(f["cz1"], 3), sh))  # the last two: the YES-view blended cushion and the 60-second-average cushion (negative = the spot is under the strike), for the exit studies
            if len(got) == 2: continue
            usable = [s for s in ("yes", "no") if 0.01 <= ask[s] <= 0.99 and not DEAD[0] <= ask[s] <= DEAD[1]]
            if not usable: continue
            st = {s: {n: state(tt, s_open, n, vals[s][n]) for n in NAMES} for s in usable}
            if "plain" not in got:  # the favourite, no checks
                fav = max(usable, key=lambda s: ask[s])
                got["plain"] = (fav, s_open, ask[fav], st[fav], vals[fav], f)
            if "model" not in got:
                ok = [s for s in usable if all(st[s][c] in (4, 5) for c in CHECKS)]  # 2026-11-22 (user: yes): an unread check is no buy; the model buys only when all five can be read and all say YES
                if ok:
                    sd = max(ok, key=lambda s: vals[s]["cushion"])
                    got["model"] = (sd, s_open, ask[sd], st[sd], vals[sd], f)
        decisions.append({"ticker": m["ticker"], "series": m["series"], "close": m["close"], "won_yes": m["won_yes"], "log": log})
        for rule, (sd, s_open, price, states, raw, f) in got.items():
            net = ((1.0 if won[sd] else 0.0) - price - fee(price)) * STAKE / price
            row = {"rule": rule, "ticker": m["ticker"], "series": m["series"], "close": m["close"], "side": sd, "price": round(price, 4), "minute": round(s_open / 60.0, 3), "won": won[sd], "net": round(net, 3), "states": states,
                   "raw": {k: (None if v is None else round(v, 4)) for k, v in raw.items()}}
            if rule == "model" or True:
                row["hr"] = hour_profile(m, sd, s_open)
            if rule == "model":
                row["chart"] = chart(m, sd, s_open, price)
                # the side's bid every 10 s after the buy to the close (for the exit study); a quote older than 30 s is None
                bids = []
                for x in range(int(s_open // 10) + 1, 91):
                    jq = bisect.bisect_right(m["qt"], m["opened"] + 10.0 * x) - 1
                    bids.append(None if jq < 0 or m["opened"] + 10.0 * x - m["qt"][jq] > 30.0 else round(m["qb"][jq] if sd == "yes" else 1.0 - m["qa"][jq], 3))
                row["bids"] = bids
            out.append(row)
    return out, len(M), decisions


rows, NM, DEC = [], 0, []
with multiprocessing.get_context("fork").Pool(4) as pool:
    for out, nm, dec in pool.imap_unordered(build, list(jobs.items())):
        rows += out; NM += nm; DEC += dec
        print(f"  an index done: {nm} markets, {time.time() - t0:.0f}s", flush=True)
Path(os.environ.get("DATA_DIR", "/data"), "priskp_buys_crypto.json.gz").write_bytes(gzip.compress(json.dumps(rows).encode()))
Path(os.environ.get("DATA_DIR", "/data"), "priskp_decisions.json.gz").write_bytes(gzip.compress(json.dumps(DEC).encode()))
model = [r for r in rows if r["rule"] == "model"]; plain = [r for r in rows if r["rule"] == "plain"]
mk = {r["ticker"] for r in model}; refused = [r for r in plain if r["ticker"] not in mk]
print(f"{NM} markets; plain buys {len(plain)}, model buys {len(model)}, refused {len(refused)}; {time.time() - t0:.0f}s\n")


def line(g):
    n = len(g)
    if n < 30: return "n %4d (too few)" % n
    win = sum(1 for r in g if r["won"]) / n
    need = sum(r["price"] + fee(r["price"]) for r in g) / n
    return "n %4d  net %+8.0f$  %+6.1fc per $1  ahead %2.0f%%  paid %.3f  need %2.0f%%  %s" % (n, sum(r["net"] for r in g), 100.0 * sum(r["net"] for r in g) / (STAKE * n), 100 * win, sum(r["price"] for r in g) / n, 100 * need, "BEAT" if win > need else "no")


print("(1) ALL: " + "\n    the model   : " + line(model) + "\n    plain order : " + line(plain) + "\n    refused     : " + line(refused))
print("\n(2) BY BUY MINUTE (minutes since the open)")
for a, b in ((2, 5), (5, 10), (10, 12), (12, 14.001)):
    print(f"  minutes {a:>2}-{int(b):<2}  model: " + line([r for r in model if a <= r["minute"] < b]) + "\n               plain : " + line([r for r in plain if a <= r["minute"] < b]))
print("\n(3) BY PRICE PAID (the side's ask; 0.45-0.55 never)")
for a, b in ((0.0, 0.20), (0.20, 0.45), (0.55, 0.65), (0.65, 0.85), (0.85, 1.0)):
    print(f"  {a:.2f}-{b:.2f}  model: " + line([r for r in model if a <= r["price"] < b]) + "\n             plain : " + line([r for r in plain if a <= r["price"] < b]))
print("\n(4) WAVE SHAPE SEGMENTS (terciles 1-3 of each reading's own previous 24 h, side view; slope 3 = rising for the side, curvature 3 = bending up)")
for lab, pool in (("model", model), ("plain", plain)):
    for w in (15, 30, 60):
        print(f"  {lab}: slope x curvature at {w} min")
        for a in (1, 2, 3):
            print(f"    slope {a}: " + " | ".join(f"curv {c}: " + line([r for r in pool if r["states"].get(f"t:sl{w}") == a and r["states"].get(f"t:cv{w}") == c]) for c in (1, 2, 3)))
    print(f"  {lab}: WAVE TURNS, 60-minute slope against 15-minute slope")
    for a in (1, 2, 3):
        print(f"    slope60 {a}: " + " | ".join(f"slope15 {c}: " + line([r for r in pool if r["states"].get("t:sl60") == a and r["states"].get("t:sl15") == c]) for c in (1, 2, 3)))
print("\n(5) EACH CHECK ON THE PLAIN BUYS, by its own fifth (1-2 NO, 3 WEAK, 4-5 YES)")
for n in CHECKS + ("volatility",):
    print(f"  --- {n}")
    for s in (1, 2, 3, 4, 5, "anomaly", "unread"): print(f"    {str(s):8s}: " + line([r for r in plain if r["states"].get(n) == s]))
print(f"\ndone in {time.time() - t0:.0f}s")
