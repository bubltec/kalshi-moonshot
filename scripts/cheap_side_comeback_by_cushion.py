"""Does a cheap side's comeback depend on the cushion against it? (2026-11-18, user, on the loss line selling sides at 5 to 7 cents that then won: "the hard line doesn't work, it needs to be time based"; "i think its really based on cushion")

One question: when a side is priced at 12 cents or under (the old loss line), how often does it go on to pay, by how far the index sits from the target against it, in the index's typical moves for the time left (the risk score's own cushion reading)?
The moments: riskrecord.py's risk_readings, every open 15-minute market every 30 s since they began (2026-11-17), joined to the markets' results. Crypto series only: a commodity's cushion was read from an outside feed until 2026-11-18 10:16 ET.
A market counts once per cushion zone, at its first reading there with a side at or under 12 cents. Holding is worth the share that paid; selling is worth the price less the fee (the mid is used: the bid is a little lower, so selling is flattered).
By curve shape: the cushion zones the score itself uses (under 0.5, 0.5 to 1.5, over 1.5), and inside each, the time left. Nothing is fitted; 12 cents and the zones are existing numbers.
Run: scripts/cloud.sh run scripts/cheap_side_comeback_by_cushion.py"""
import json
import math
from collections import defaultdict

from kalshi_moonshot import config, db, riskscore, variants
from kalshi_moonshot.strategy import fee_per_contract

LINE = 0.12
s = config.load()
main = db.connect(s.db_path, "main", shared=True)
ci, pi = riskscore.NAMES.index("cushion"), riskscore.NAMES.index("price")
rows = main.execute("SELECT r.ticker, r.ts, r.x, r.age, w.result FROM risk_readings r JOIN windows w ON w.ticker = r.ticker WHERE w.result IN ('yes','no') ORDER BY r.ts").fetchall()
first = {}
n_read = 0
for r in rows:
    series = r["ticker"].split("-")[0]
    if not variants.crypto_index(series):
        continue
    try:
        x = json.loads(r["x"])
    except (ValueError, TypeError):
        continue
    if len(x) != len(riskscore.NAMES):
        continue
    n_read += 1
    mid = 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, x[pi]))))
    if mid <= LINE:
        side, price, against = "yes", mid, -x[ci]
    elif mid >= 1.0 - LINE:
        side, price, against = "no", 1.0 - mid, x[ci]
    else:
        continue
    left = 900.0 - float(r["age"] or 0.0)
    if left <= 0 or price <= 0.005:
        continue
    zone = "cushion against it under 0.5 (close)" if against < 0.5 else "cushion against it 0.5 to 1.5 (fair)" if against < 1.5 else "cushion against it over 1.5 (wide)"
    late = "over 3 min left" if left > 180 else "1 to 3 min left" if left > 60 else "under 1 min left"
    for key in ((zone, "all"), (zone, late)):
        k = (r["ticker"], side, key)
        if k not in first:
            first[k] = (price, r["result"] == side)
groups = defaultdict(list)
for (_, _, key), v in first.items():
    groups[key].append(v)
print(f"window: every risk reading with a result, crypto series; {n_read} readings on {len({r['ticker'] for r in rows})} markets; a side at or under {LINE:.2f}")
for zone in ("cushion against it under 0.5 (close)", "cushion against it 0.5 to 1.5 (fair)", "cushion against it over 1.5 (wide)"):
    print(f"--- {zone}")
    for late in ("all", "over 3 min left", "1 to 3 min left", "under 1 min left"):
        g = groups.get((zone, late))
        if not g:
            continue
        n, paid = len(g), sum(1 for _, w in g if w)
        price = sum(p for p, _ in g) / n
        sell = sum(p - fee_per_contract(p) for p, _ in g) / n
        print(f"  {late:17s} markets {n:4d} | paid {paid:3d} ({100.0 * paid / n:4.1f}%) | avg price {price:.3f} | hold is worth {paid / n:.3f} a contract, selling {sell:.3f} | holding beats selling by {paid / n - sell:+.3f}")
print("DONE")
