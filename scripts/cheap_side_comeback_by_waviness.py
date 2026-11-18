"""Does a wavy path make a cheap side's comeback more likely? (2026-11-18, user: "it also depends on the lookback. if the path is wavy it means there's likely a comeback, which will at bare minimum get us a better sale")

One question on the same moments as scripts/cheap_side_comeback_by_cushion.py: a side priced at 12 cents or under, in every recorded risk reading with a result since 2026-11-17, crypto series. Split by how wavy the market has been SO FAR: the number of times the favorite has changed in this market up to that moment (the risk score's own flip-rate reading, times the minutes gone).
Groups: no change of favorite yet; some, at or under the series' own median count among its cheap-side moments; over that median (the wavy ones). The median is each series' own, from these readings.
A market counts once per group, at its first cheap reading there. Reported: how often the side paid; holding against selling at the mid less the fee; and the better sale: the side's highest later price in the market (30 s marks), and how often it at least doubled.
Also split by time left (over 3 minutes, and 3 minutes or under), since the cushion study found the time left decides most.
Run: scripts/cloud.sh run scripts/cheap_side_comeback_by_waviness.py"""
import json
import math
from collections import defaultdict
from statistics import median

from kalshi_moonshot import config, db, riskscore, variants
from kalshi_moonshot.strategy import fee_per_contract

LINE = 0.12
s = config.load()
main = db.connect(s.db_path, "main", shared=True)
pi, fi = riskscore.NAMES.index("price"), riskscore.NAMES.index("flip rate")
rows = main.execute("SELECT r.ticker, r.ts, r.x, r.age, w.result FROM risk_readings r JOIN windows w ON w.ticker = r.ticker WHERE w.result IN ('yes','no') ORDER BY r.ts").fetchall()
path = defaultdict(list)  # ticker -> (ts, yes mid)
cheap = []
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
    mid = 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, x[pi]))))
    path[r["ticker"]].append((float(r["ts"]), mid))
    if mid <= LINE:
        side, price = "yes", mid
    elif mid >= 1.0 - LINE:
        side, price = "no", 1.0 - mid
    else:
        continue
    age = float(r["age"] or 0.0)
    left = 900.0 - age
    if left <= 0 or price <= 0.005 or abs(x[pi]) < 1e-6:
        continue
    flips = round(abs(x[fi] / x[pi]) * age / 60.0)  # the reading is flips per minute gone (capped at 1.5) times the price's logit
    cheap.append((series, r["ticker"], float(r["ts"]), side, price, left, flips, r["result"] == side))
med = {}
for ser in {c[0] for c in cheap}:
    some = [c[6] for c in cheap if c[0] == ser and c[6] > 0]
    med[ser] = median(some) if some else 0
first = {}
for ser, ticker, ts, side, price, left, flips, paid in cheap:
    wav = "no change of favorite yet" if flips == 0 else "some changes (at or under the series' median)" if flips <= med[ser] else "wavy (over the series' median)"
    later = [(m if side == "yes" else 1.0 - m) for t, m in path[ticker] if t > ts]
    best = max(later) if later else price
    for key in ((wav, "all"), (wav, "over 3 min left" if left > 180 else "3 min or under")):
        k = (ticker, side, key)
        if k not in first:
            first[k] = (price, paid, best)
groups = defaultdict(list)
for (_, _, key), v in first.items():
    groups[key].append(v)
print(f"window: every risk reading with a result since they began, crypto series; {len(cheap)} cheap-side readings on {len({c[1] for c in cheap})} markets; a side at or under {LINE:.2f}; each series' median count of favorite changes among them: {dict(sorted(med.items()))}")
for wav in ("no change of favorite yet", "some changes (at or under the series' median)", "wavy (over the series' median)"):
    print(f"--- {wav}")
    for late in ("all", "over 3 min left", "3 min or under"):
        g = groups.get((wav, late))
        if not g:
            continue
        n, paid = len(g), sum(1 for _, w, _ in g if w)
        price = sum(p for p, _, _ in g) / n
        sell = sum(p - fee_per_contract(p) for p, _, _ in g) / n
        best = sum(b for _, _, b in g) / n
        dbl = sum(1 for p, _, b in g if b >= 2.0 * p)
        print(f"  {late:16s} markets {n:4d} | paid {paid:3d} ({100.0 * paid / n:4.1f}%) | avg price {price:.3f} | hold {paid / n:.3f} vs sell {sell:.3f}: {paid / n - sell:+.3f} | highest later price avg {best:.3f} | at least doubled {dbl:3d} ({100.0 * dbl / n:4.1f}%)")
print("DONE")
