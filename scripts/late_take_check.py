"""What do the live profit takes at the very end cost and save? (2026-11-15 night, user: "we keep selling our profit at the last second, always taking a fee")

One question, same bets (CLAUDE.md rule 4): every live crypto_multiscale bet sold by the profit take since 2026-11-14 17:02 ET, split by the bid it sold at (under 0.90, 0.90 to 0.95, 0.95 and over) and by the seconds left at the sale (over 120, 60 to 120, under 60). For each: n, the sell fee paid, the sale's value after that fee, what holding the same contracts to settlement paid (no fee at settlement), and the difference (sale less hold), plus how many times holding would have paid less than the sale (the take saved the bet) and more. Settlement from Kalshi's result, bets whose market has not settled are left out. Not split by curve shape. Fee at a price p is 0.07 x p x (1 - p) a contract, so it is under a cent from 0.95 up.

Run on the host: python scripts/late_take_check.py
"""
import datetime
import json
import urllib.request
from collections import defaultdict

from kalshi_moonshot import config, db
from kalshi_moonshot.strategy import fee_per_contract

ET = datetime.timezone(datetime.timedelta(hours=-5))
SINCE = datetime.datetime(2026, 11, 14, 17, 2, tzinfo=ET).timestamp()
s = config.load()
st = db.connect(s.db_path, "main", shared=True)
cache = {}


def result(tk):
    if tk not in cache:
        try:
            cache[tk] = json.load(urllib.request.urlopen(f"https://api.elections.kalshi.com/trade-api/v2/markets/{tk}", timeout=10))["market"].get("result")
        except Exception:
            cache[tk] = None
    return cache[tk]


g = defaultdict(list)
for r in st.execute("SELECT ticker, side, contracts, stake_usd, exit_price, exit_ts, close_time, outcome FROM decisions WHERE lane='crypto_multiscale' AND mode='trade' AND action='bet' AND stake_usd>0 AND exit_price IS NOT NULL AND exit_reason LIKE '%profit take%' AND ts>?", (SINCE,)).fetchall():
    n, cost = float(r["contracts"]), float(r["stake_usd"]); x = float(r["exit_price"])
    res = r["outcome"] if r["outcome"] in ("yes", "no") else result(r["ticker"])
    if res not in ("yes", "no"):
        continue
    close = datetime.datetime.fromisoformat(str(r["close_time"]).replace("Z", "+00:00")).timestamp()
    left = close - float(r["exit_ts"])
    band = "bid under 0.90" if x < 0.90 else "bid 0.90 to 0.95" if x < 0.95 else "bid 0.95 and over"
    when = "over 120 s left" if left > 120 else "60 to 120 s left" if left > 60 else "under 60 s left"
    fee = n * fee_per_contract(x)
    sale = n * x - fee - cost
    hold = (n if res == r["side"] else 0.0) - cost
    g[(band, when)].append((fee, sale, hold))
    g[(band, "all times")].append((fee, sale, hold))
print("profit takes by the bid they sold at and the time left: n | total sell fee | sale after fee | holding to settlement | sale less hold | holding paid less (the take saved the bet) / more")
for key in sorted(g, key=lambda k: (k[0], ["over 120 s left", "60 to 120 s left", "under 60 s left", "all times"].index(k[1]))):
    x = g[key]
    print(f"  {key[0]:18s} {key[1]:16s}: n {len(x):3d} | fee ${sum(a[0] for a in x):5.2f} | sale ${sum(a[1] for a in x):+8.2f} | hold ${sum(a[2] for a in x):+8.2f} | diff ${sum(a[1] - a[2] for a in x):+7.2f} | {sum(1 for a in x if a[2] < a[1] - 0.01)} / {sum(1 for a in x if a[2] > a[1] + 0.01)}")
print("DONE")
