"""SETTLEMENT RULE CHECK with Kalshi's own numbers (2026-11-20, user via parent; the first audit used our open tick as the strike and found the last-minute average agreeing with the result less often than the last tick). windows.strike is Kalshi's floor_strike and windows.settled_value is Kalshi's expiration_value (the underlying's value the market settled on). For commodity windows (perp reference, S&P out) that have both: (1) is Kalshi's strike our open tick? (2) which perp statistic is Kalshi's settled value: the last tick, the average of the last 60 s, or another? (median absolute gap and the share within 1e-6 relative) (3) the result against settled_value vs strike, and against each statistic vs Kalshi's strike. Run: SPOT=0 scripts/cloud_job.sh scripts/binary_risk_settlement_check.py"""
import bisect, math, statistics
from datetime import datetime, timezone
from kalshi_moonshot import config, db
from kalshi_moonshot.perpref import index_for_series as perp_index
from kalshi_moonshot.kalshiws import index_for_series as crypto_index

s = config.reload()
st = db.connect(s.db_path, "main", shared=True)
tape = db.connect(s.tape_path, "tape", shared=True)
START = datetime(2026, 11, 9, 5, tzinfo=timezone.utc).timestamp()
ws = st.execute("SELECT ticker, series, close_ts, strike, result, settled_value FROM windows WHERE result IN ('yes','no') AND close_ts > ? ORDER BY close_ts", (START,)).fetchall()
ws = [w for w in ws if not w["series"].startswith("KXINX") and not crypto_index(w["series"]) and perp_index(w["series"])]
print(f"{len(ws)} commodity windows; with Kalshi strike {sum(1 for w in ws if w['strike'])}, with settled_value {sum(1 for w in ws if w['settled_value'])}")
print("sample rows:", [(w["ticker"], w["strike"], w["settled_value"], w["result"]) for w in ws[:3]])
rows = []
for w in ws[::max(1, len(ws) // 400)]:
    if not w["strike"] or not w["settled_value"]: continue
    ix = perp_index(w["series"]); close = float(w["close_ts"]); opened = close - 900.0
    tk = tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND ts BETWEEN ? AND ? ORDER BY ts", (ix, opened - 5, close + 5)).fetchall()
    T, V = [float(r[0]) for r in tk], [float(r[1]) for r in tk]
    if len(T) < 800: continue
    k0 = bisect.bisect_left(T, opened)
    open_tick = V[k0] if k0 < len(V) else None
    j = bisect.bisect_right(T, close) - 1
    last_tick = V[j]
    stats = {"last tick": last_tick}
    for sec in (60, 30, 120):
        seg = [v for t, v in zip(T, V) if close - sec <= t <= close]
        if seg: stats[f"avg last {sec}s"] = statistics.fmean(seg)
    seg = [v for t, v in zip(T, V) if close - 60 <= t < close]
    if seg: stats["avg 60s before close (excl. close tick)"] = statistics.fmean(seg)
    rows.append((w, open_tick, stats))
print(f"{len(rows)} windows compared")
def rel(a, b): return abs(a - b) / b
print("\n(1) Kalshi strike vs our open tick: median relative gap %.2e, share equal within 1e-6: %.2f" % (statistics.median(rel(w["strike"], o) for w, o, _ in rows if o), sum(1 for w, o, _ in rows if o and rel(w["strike"], o) < 1e-6) / len(rows)))
print("\n(2) Kalshi settled value vs perp statistic (median relative gap, share within 1e-5):")
for name in rows[0][2]:
    g = [rel(float(w["settled_value"]), st_[name]) for w, _, st_ in rows if name in st_]
    print(f"  {name:42s} median gap {statistics.median(g):.2e}  within 1e-5: {sum(1 for x in g if x < 1e-5) / len(g):.2f}  (n {len(g)})")
print("\n(3) the result against the rule:")
agree = sum(1 for w, _, _ in rows if (float(w["settled_value"]) >= w["strike"]) == (w["result"] == "yes"))
print(f"  settled_value >= strike agrees with the result: {agree}/{len(rows)}")
for name in rows[0][2]:
    a = sum(1 for w, _, st_ in rows if name in st_ and (st_[name] >= w["strike"]) == (w["result"] == "yes"))
    print(f"  {name:42s} vs KALSHI strike agrees: {a}/{len(rows)}")
