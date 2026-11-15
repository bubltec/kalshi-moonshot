"""Paper variants against the live crypto_multiscale lane since the width change (2026-11-14 17:02 ET), by their own exits and held to settlement, all entries and only the live lane's band 0.57 to 0.82. Not the same bets: the variants have other bands and rules (always=true, price 0.70 to 0.90) and paper fills are optimistic. Run on the host: python scripts/lane_compare_paper_live.py"""
import datetime, json, time, urllib.request
from collections import defaultdict
from kalshi_moonshot import config, db
from kalshi_moonshot.strategy import fee_per_contract
s = config.load(); st = db.connect(s.db_path, "main", shared=True); tape = db.connect(s.tape_path, "tape", shared=True)
ET = datetime.timezone(datetime.timedelta(hours=-5))
since = datetime.datetime(2026, 11, 14, 17, 2, tzinfo=ET).timestamp(); now = time.time()
res_cache = {}
def result(tk):
    if tk not in res_cache:
        g = tape.execute("SELECT bid, ask FROM quotes WHERE ticker=? ORDER BY ts DESC LIMIT 1", (tk,)).fetchone()
        res_cache[tk] = None if not g else ("yes" if float(g[0]) >= 0.95 else "no" if float(g[1]) <= 0.05 else None)
    return res_cache[tk]
rows = defaultdict(list)
for r in st.execute("SELECT lane, ts, ticker, contracts, stake_usd, exit_price, outcome, side FROM decisions WHERE action='bet' AND stake_usd>0 AND lane LIKE 'crypto_multiscale%' AND ts>?", (since,)).fetchall():
    n, cost = float(r["contracts"] or 0), float(r["stake_usd"])
    if n <= 0: continue
    entry = cost / n
    res = r["outcome"] if r["outcome"] in ("yes", "no") else result(r["ticker"])
    if res not in ("yes", "no"): continue
    held = (n if res == r["side"] else 0.0) - cost
    real = (n * (float(r["exit_price"]) - fee_per_contract(float(r["exit_price"]))) - cost) if r["exit_price"] is not None else held
    rows[r["lane"]].append((entry, cost, real, held, r["ts"], r["ticker"]))
def line(name, x):
    if not x: return
    c = sum(a[1] for a in x)
    print(f"  {name:30s} n {len(x):3d} | own exits {sum(a[2] for a in x):+8.2f} ({sum(a[2] for a in x)/c*100:+5.1f}%) | held to settlement {sum(a[3] for a in x):+8.2f} ({sum(a[3] for a in x)/c*100:+5.1f}%) | won {sum(1 for a in x if a[3] > 0)}/{len(x)} | avg entry {sum(a[0] for a in x)/len(x):.2f}")
print("since 11-14 17:02 ET, settled bets, ALL entry prices:")
for lane in sorted(rows): line(lane, rows[lane])
print("\nonly entries inside the live lane's band 0.57 to 0.82:")
for lane in sorted(rows): line(lane, [a for a in rows[lane] if 0.57 <= a[0] <= 0.82])
print("\nonly entries 0.57 to 0.82, the live lane's own bets vs markets the live lane bought that a paper variant also bought (same ticker):")
live = {a[5] for a in rows["crypto_multiscale"]}
for lane in sorted(rows):
    if lane == "crypto_multiscale": continue
    line(lane + " (same markets)", [a for a in rows[lane] if a[5] in live and 0.57 <= a[0] <= 0.82])
print("DONE")
