"""What each rule actually changed, on the decisions it touched, since it went live (user, 2026-11-15 night: "those changes don't correlate to the losses in those periods necessarily").

The forward scoreboard reports the whole book after each change, which is mostly the market of that hour. This scores only what a rule touched. A rule that blocks a buy leaves a pass row in the decisions table with its reason; for each distinct market and side it blocked since the rule went live, the return the buy would have had HELD TO SETTLEMENT (1 or 0 over the all-in cost, ask plus fee, less 1), from Kalshi's result. The same measure on the live bets actually taken in the same period is the comparison (their return if held to settlement at their own entry), so both sides ignore the stops and takes and the market's mood is the same for both. Blocked set against taken set, per dollar and by share that paid:
  max_same_side = 2 (live 20:44): pass rows "same side held".
  curve edge floor -0.03 (live 23:35): pass rows whose edge is between -0.05 and -0.03 (newly blocked; under -0.05 was blocked before).
  curve edge floor -0.05, the earlier floor, since 20:44, for a larger blocked sample.
Pass rows are kept for some scans, not all, so n is what the table says. A rule that blocks bets that would have paid less than the ones it lets in is working; one that blocks winners is costing. Not split by curve shape.

Run on the host: python scripts/forward_effects.py
"""
import datetime
import json
import re
import urllib.request

from kalshi_moonshot import config, db
from kalshi_moonshot.strategy import fee_per_contract

ET = datetime.timezone(datetime.timedelta(hours=-5))
cut = lambda h, m: datetime.datetime(2026, 11, 15, h, m, tzinfo=ET).timestamp()
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


def hold_return(ticker, side, ask):
    res = result(ticker)
    if res not in ("yes", "no") or not ask or not (0.01 < ask < 0.99):
        return None
    return (1.0 if res == side else 0.0) / (ask + fee_per_contract(ask)) - 1.0


def stats(name, rets):
    rets = [r for r in rets if r is not None]
    if not rets:
        print(f"  {name:48s} n   0")
        return
    print(f"  {name:48s} n {len(rets):3d} | paid {sum(1 for r in rets if r > 0) / len(rets):4.0%} | held-to-settlement return per $ {sum(rets) / len(rets) * 100:+6.1f}%")


def blocked(t0, pred):
    seen, out = set(), []
    for r in st.execute("SELECT ts, ticker, side, ask, reason FROM decisions WHERE lane='crypto_multiscale' AND action='pass' AND ts>? ORDER BY ts", (t0,)).fetchall():
        txt = str(r["reason"] or "")
        if not pred(txt):
            continue
        k = (r["ticker"], r["side"])
        if k in seen:
            continue
        seen.add(k)
        out.append(hold_return(r["ticker"], r["side"], float(r["ask"] or 0)))
    return out


def taken(t0):
    out = []
    for r in st.execute("SELECT ticker, side, contracts, stake_usd FROM decisions WHERE lane='crypto_multiscale' AND mode='trade' AND action='bet' AND stake_usd>0 AND ts>?", (t0,)).fetchall():
        n, cost = float(r["contracts"] or 0), float(r["stake_usd"])
        if n > 0:
            out.append(hold_return(r["ticker"], r["side"], cost / n))
    return out


def edge_in(lo, hi):
    def p(txt):
        m = re.search(r"\[curve edge ([+-]?\d+\.\d+)\]", txt)
        return bool(m) and "under the ask" in txt and lo <= float(m.group(1)) < hi
    return p


print("WHAT EACH RULE CHANGED, on the decisions it touched, since it went live (blocked buys' return if held to settlement against the live buys taken in the same period)")
t = cut(20, 44)
print("\nmax_same_side = 2, since 20:44:")
stats("blocked: 'same side held' (distinct market and side)", blocked(t, lambda x: "same side held" in x))
stats("taken: the live lane's own bets", taken(t))
print("\ncurve edge floor, the earlier -0.05, since 20:44:")
stats("blocked: edge under -0.05", blocked(t, edge_in(-9, -0.05)))
stats("taken: the live lane's own bets", taken(t))
t = cut(23, 35)
print("\ncurve edge floor -0.03, since 23:35 (newly blocked: -0.05 to -0.03):")
stats("blocked: edge -0.05 to -0.03", blocked(t, edge_in(-0.05, -0.03)))
stats("taken: the live lane's own bets", taken(t))
print("DONE")
