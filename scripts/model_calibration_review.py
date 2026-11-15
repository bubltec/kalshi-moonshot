"""Is the lane's chance calibrated, and does the hourly width factor (widthfit k) move it? (2026-11-15, user: "i also want to check on our hourly review and the factor we update hourly. how is that working and how is it impacting us?")
Every live crypto_multiscale buy since 14:30 ET 2026-11-14: the chance the lane bet on (decisions.model_p, the side's own), the price it paid, and whether the market settled in the side's favor. Calibration by buckets of the claimed chance (claimed mean against the share that won), and the claimed edge (chance less the ask) against the realized one (win less the ask). Then the last 7 hours split by the hourly width factor in force at each buy (the "hour width correction" lines in the bot log, k from 0.50 to 2.00, refit every 15 minutes): the same comparison for k at or under 0.625 against k at 0.75 or over.
Run on the host: python scripts/model_calibration_review.py  (K_LINES: the log lines are read by the caller, see below)"""
import datetime
import json
import os
import re
import urllib.request

from kalshi_moonshot import config, db

ET = datetime.timezone(datetime.timedelta(hours=-5))
SINCE = datetime.datetime(2026, 11, 14, 14, 30, tzinfo=ET).timestamp()
s = config.load()
st = db.connect(s.db_path, "main", shared=True)
rows = st.execute("SELECT ts, ticker, side, ask, model_p, contracts, stake_usd, exit_price, outcome FROM decisions WHERE lane='crypto_multiscale' AND mode='trade' AND action='bet' AND stake_usd>0 AND ts>? ORDER BY ts", (SINCE,)).fetchall()
cache = {}


def result_of(ticker, outcome):
    if outcome in ("yes", "no"):
        return outcome
    if ticker not in cache:
        try:
            with urllib.request.urlopen(f"https://api.elections.kalshi.com/trade-api/v2/markets/{ticker}", timeout=10) as f:
                r = json.load(f)["market"].get("result")
            cache[ticker] = r if r in ("yes", "no") else None
        except Exception:
            cache[ticker] = None
    return cache[ticker]


bets = []
for r in rows:
    res = result_of(r["ticker"], r["outcome"])
    if res is None or r["model_p"] is None:
        continue
    entry = float(r["stake_usd"]) / float(r["contracts"])
    bets.append(dict(t=float(r["ts"]), p=float(r["model_p"]), entry=entry, won=1.0 if res == r["side"] else 0.0))
print(f"live buys since 14:30 ET 11-14 with a chance and a result: {len(bets)}; claimed chance averages {sum(b['p'] for b in bets)/len(bets):.3f}, paid {sum(b['entry'] for b in bets)/len(bets):.3f}, won {sum(b['won'] for b in bets)/len(bets):.3f}")


def show(title, grp):
    print(f"\n{title}")
    for lo, hi in ((0.0, 0.70), (0.70, 0.85), (0.85, 0.95), (0.95, 1.01)):
        g = [b for b in grp if lo <= b["p"] < hi]
        if g:
            print(f"  claimed {lo:.2f}-{hi:.2f}: n={len(g):3d} claimed {sum(b['p'] for b in g)/len(g):.3f} paid {sum(b['entry'] for b in g)/len(g):.3f} WON {sum(b['won'] for b in g)/len(g):.3f} | claimed edge {sum(b['p']-b['entry'] for b in g)/len(g):+.3f} realized edge {sum(b['won']-b['entry'] for b in g)/len(g):+.3f}")
    if grp:
        print(f"  all: n={len(grp)} claimed edge {sum(b['p']-b['entry'] for b in grp)/len(grp):+.3f} realized edge {sum(b['won']-b['entry'] for b in grp)/len(grp):+.3f}")


show("ALL buys since 14:30 ET 11-14", bets)
ks = []
for line in os.environ.get("K_LINES", "").split(";"):
    m = re.match(r"(\d\d):(\d\d) ([0-9.]+)", line.strip())
    if m:
        ks.append((int(m.group(1)), int(m.group(2)), float(m.group(3))))
if ks:
    base = datetime.datetime.fromtimestamp(SINCE, ET).replace(hour=0, minute=0, second=0, microsecond=0) + datetime.timedelta(days=1)
    fits = sorted((base.replace(hour=h, minute=m).timestamp(), k) for h, m, k in ks)
    def k_at(t):
        got = [k for ts, k in fits if ts <= t]
        return got[-1] if got else None
    recent = [b for b in bets if b["t"] >= base.timestamp() and k_at(b["t"]) is not None]
    show(f"since 00:16 ET 11-15, k at or under 0.625 (the calm-hour fit): {sum(1 for b in recent if k_at(b['t']) <= 0.625)} buys", [b for b in recent if k_at(b["t"]) <= 0.625])
    show(f"since 00:16 ET 11-15, k at 0.75 or over: {sum(1 for b in recent if k_at(b['t']) >= 0.75)} buys", [b for b in recent if k_at(b["t"]) >= 0.75])
print("DONE")
