"""Do the buys that fill differ from the buys that do not? (2026-11-16, user: "it looks like it's trying to buy in a time where everyone else is. How can we get an edge?")

One question, same decisions (CLAUDE.md rule 4): every buy the live crypto_multiscale lane tried since 2026-11-14 17:02 ET: the ones that filled (a bet row) and the ones that rested 5 seconds, did not fill and were not retaken (a pass row "resting buy ... did not fill ... nothing bought"), one per distinct market and side. For each, the return the buy would have had HELD TO SETTLEMENT at its ask (1 or 0 over ask plus fee, less 1, from Kalshi's result): filled against missed. A resting buy fills when the price comes to it and misses when the price runs away, so if the misses paid more than the fills, passive buying is adverse selection: it gets the bets that went against it and loses the ones that ran. Also where in the market the attempts fall (minutes into the market) and the share that missed, and the cost the resting saves (the buy fee a maker does not pay and the cent under the ask) against what the misses were worth. Not split by curve shape. Held to settlement for both sides, so stops and takes do not muddy it.

Run on the host: python scripts/fill_selection_study.py
"""
import datetime
import json
import re
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


def ret(ticker, side, ask):
    res = result(ticker)
    if res not in ("yes", "no") or not (0.01 < ask < 0.99):
        return None
    return (1.0 if res == side else 0.0) / (ask + fee_per_contract(ask)) - 1.0


def elapsed(ticker, ts):
    m = re.match(r"KX[A-Z]+15M-26NOV(\d\d)(\d\d)(\d\d)-", ticker)
    if not m:
        return None
    close = datetime.datetime(2026, 11, int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=ET).timestamp()
    return 900.0 - (close - ts)


filled, missed = {}, {}
for r in st.execute("SELECT ts, ticker, side, contracts, stake_usd FROM decisions WHERE lane='crypto_multiscale' AND mode='trade' AND action='bet' AND stake_usd>0 AND ts>? ORDER BY ts", (SINCE,)).fetchall():
    n, cost = float(r["contracts"] or 0), float(r["stake_usd"])
    if n > 0:
        filled.setdefault((r["ticker"], r["side"]), (r["ts"], cost / n))
for r in st.execute("SELECT ts, ticker, side, ask, reason FROM decisions WHERE lane='crypto_multiscale' AND action='pass' AND ts>? AND reason LIKE '%did not fill%nothing bought%' ORDER BY ts", (SINCE,)).fetchall():
    k = (r["ticker"], r["side"])
    if k not in filled:
        m = re.search(r"the ask was ([\d.]+)", str(r["reason"]))
        missed.setdefault(k, (r["ts"], float(m.group(1)) if m else float(r["ask"] or 0)))


def table(name, d):
    rs = [(ret(k[0], k[1], v[1]), elapsed(k[0], v[0])) for k, v in d.items()]
    ok = [x for x, _ in rs if x is not None]
    print(f"  {name:8s}: attempts {len(d):3d}, settled {len(ok):3d} | paid {sum(1 for x in ok if x > 0) / max(len(ok), 1):4.0%} | held-to-settlement return per $ {sum(ok) / max(len(ok), 1) * 100:+6.1f}%")
    return rs


print("every distinct buy attempt since the width change, held to settlement at its ask:")
a = table("filled", filled)
b = table("missed", missed)
print("\nwhere in the market the attempts fall (minutes into the market): attempts | missed share | held return of the missed")
buckets = ((0, 3), (3, 6), (6, 9), (9, 12), (12, 15))
for lo, hi in buckets:
    f_ = [k for k, v in filled.items() if elapsed(k[0], v[0]) is not None and lo * 60 <= elapsed(k[0], v[0]) < hi * 60]
    m_ = [k for k, v in missed.items() if elapsed(k[0], v[0]) is not None and lo * 60 <= elapsed(k[0], v[0]) < hi * 60]
    mr = [ret(k[0], k[1], missed[k][1]) for k in m_]
    mr = [x for x in mr if x is not None]
    print(f"  {lo:2d}-{hi:2d} min: attempts {len(f_) + len(m_):3d} | missed {len(m_) / max(len(f_) + len(m_), 1):4.0%} | missed paid {sum(1 for x in mr if x > 0) / max(len(mr), 1):4.0%} return {sum(mr) / max(len(mr), 1) * 100:+6.1f}% (n {len(mr)})")
print("DONE")
