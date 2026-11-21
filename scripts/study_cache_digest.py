"""ROW DIGEST (2026-11-21): the rows of scripts/binary_risk_fit_search.py V=base (its own build, unchanged), reduced to numbers a cache rebuild can be compared with: row count, per-segment sums of ask, the five nets and every side-vector column, and the commodity rows of three sample markets. Printed in the form scripts/study_cache_digest_local.py prints from the cache. Run: SPOT=0 scripts/cloud_job.sh scripts/study_cache_digest.py"""
import bisect, math, multiprocessing, os, time
from datetime import datetime, timezone
from kalshi_moonshot import binaryrisk as br, config, db
from kalshi_moonshot.perpref import index_for_series as perp_index
from kalshi_moonshot.kalshiws import index_for_series as crypto_index
from kalshi_moonshot.strategy import fee_per_contract as fee

V = os.environ.get("V", "base")
HOLDOUT = os.environ.get("HOLDOUT") == "1"
POOL = V == "pool"
WIN = {"win240": 240.0, "winall": 1e6}.get(V, 120.0)
t0 = time.time()
BAND, TOPS, STEP, HORIZONS = (0.60, 0.85), (0.05, 0.10), 60.0, br.HORIZONS
BANDS = ((0.60, 0.65), (0.65, 0.70), (0.70, 0.75), (0.75, 0.80), (0.80, 0.85))
START = datetime(2026, 11, 9, 5, tzinfo=timezone.utc).timestamp()
NI = {n: i for i, n in enumerate(br.NAMES)}
FEWER = [NI[n] for n in ("logit_ask", "czb", "tr13", "left", "pos15", "pos30", "mv15", "mv30", "dr24")]
s = config.reload()
st = db.connect(s.db_path, "main", shared=True)
wins = st.execute("SELECT ticker, series, close_ts, strike, result FROM windows WHERE result IN ('yes','no') AND close_ts > ? ORDER BY close_ts", (START + 7200,)).fetchall()
jobs = {}
for w in wins:
    if w["series"].startswith("KXINX"): continue
    ix = crypto_index(w["series"])
    kind = "crypto" if ix else "commodity"
    if not ix: ix = perp_index(w["series"])
    if not ix or (kind == "crypto" and (not POOL or not w["strike"])): continue
    jobs.setdefault(ix, []).append((w["ticker"], float(w["close_ts"]), float(w["strike"]) if kind == "crypto" else None, w["result"] == "yes", kind))

def build(job):
    ix, ws = job
    tape = db.connect(config.reload().tape_path, "tape", shared=True)
    tk = tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND ts BETWEEN ? AND ? ORDER BY ts", (ix, min(w[1] for w in ws) - 4900, max(w[1] for w in ws) + 5)).fetchall()
    T, Vv = [float(r[0]) for r in tk], [float(r[1]) for r in tk]
    del tk
    rows = []
    for ticker, close, strike, won_yes, kind in ws:
        opened = close - 900.0
        if strike is None:
            k = bisect.bisect_left(T, opened)
            if k >= len(T) or T[k] - opened > 25.0: continue
            strike = Vv[k]
        ref = []
        for hh in (24.0, 72.0):
            rr = tape.execute("SELECT value FROM index_ticks WHERE index_id=? AND ts<=? AND ts>? ORDER BY ts DESC LIMIT 1", (ix, opened - hh * 3600.0, opened - hh * 3600.0 - 1800.0)).fetchone()
            ref.append(float(rr[0]) if rr else 0.0)
        q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (ticker, opened - 60, close)).fetchall()
        if len(q) < 20: continue
        qt, qb, qa = [float(r[0]) for r in q], [float(r[1]) for r in q], [float(r[2]) for r in q]
        scale = 100.0 if max(qa) > 1.5 else 1.0
        def quote_at(t):
            j = bisect.bisect_right(qt, t) - 1
            return (qb[j] / scale, qa[j] / scale) if j >= 0 and t - qt[j] <= 60.0 else None
        t = opened + 60.0
        while t <= close - 15.0:
            qq = quote_at(t)
            f = br.readings(T, Vv, t, strike, close - t, (ref[0], ref[1])) if qq else None
            if f and qq:
                ahead = [quote_at(t + h) if t + h <= close - 15.0 else None for h in HORIZONS]
                for side, ask in (("yes", qq[1]), ("no", 1.0 - qq[0])):
                    if not 0.02 <= ask <= 0.98: continue
                    sg = 1.0 if side == "yes" else -1.0
                    if min(sg * f["cz1"], sg * f["cz3"], sg * f["cz5"]) <= 0.0 or close - t < 180.0: continue
                    won = won_yes if side == "yes" else not won_yes
                    pays = [1.0 if won else 0.0]
                    for e in ahead:
                        if e is None: pays.append(1.0 if won else 0.0)
                        else:
                            sell = e[0] if side == "yes" else 1.0 - e[1]
                            pays.append(sell - fee(sell))
                    rows.append((t, br.side_vector(f, side, ask), [(p - ask - fee(ask)) / ask for p in pays], ask, ticker, side, close - t, 1.0 if won else 0.0, close, kind))
            t += STEP
    return rows

with multiprocessing.get_context("fork").Pool(4) as pool:
    S = [r for rs in pool.imap_unordered(build, list(jobs.items())) for r in rs]
S.sort(key=lambda r: r[0])

import json
def digest(S):
    cm = [r for r in S if r[9] == "commodity"]
    out = {"n": len(cm), "ask": sum(r[3] for r in cm), "won": sum(r[7] for r in cm), "left": sum(r[6] for r in cm), "nets": [sum(r[2][e] for r in cm) for e in range(5)], "vec": [sum(r[1][i] for r in cm) for i in range(len(br.NAMES))]}
    ks = sorted({(r[4], r[5]) for r in cm})[::400]
    out["samples"] = {f"{k[0]}|{k[1]}": [[round(r[0], 3), round(r[3], 6), round(r[2][0], 6), round(r[1][2], 6), round(r[1][14], 6)] for r in cm if (r[4], r[5]) == k][:3] for k in ks}
    return out
print("DIGEST " + json.dumps(digest(S)))
