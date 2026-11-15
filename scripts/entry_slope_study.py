"""The entry trend check read as a slope, not as two points (2026-11-14 night, user: "this doesn't help you understand how the tick in that moment is sloping though").
Two readings of the side's price (mid of bid and ask, in the side's own terms) at the moment of each live crypto_multiscale buy since Mon 2026-11-09 00:00 ET, over the last 60, 120, 180 and 240 s:
  (a) the endpoint check (live shape): the price now is above its price `span` seconds ago;
  (b) the slope the exit uses (curverisk.still_rising, any rise): the price now is above where the window started AND within the market's noise of the window's high (the typical minute over the last 15 minutes x sqrt(span / 60)), so a price that rose and has turned down does not pass;
  (c) both: (a) at 180 s and (b) at 60 s: gaining over three minutes and still rising now.
For each: the buys kept and the buys dropped, n, net dollars and net per $1 staked (real exit or settlement, fee on both legs). Spans chosen after the earlier study; said so.
Run on the host: python scripts/entry_slope_study.py"""
import bisect
import datetime
import os

from kalshi_moonshot import config, curverisk, db, quoteshock
from kalshi_moonshot.strategy import fee_per_contract

SPANS = tuple(float(x) for x in os.environ.get("SPANS", "60,120,180,240").split(","))  # the look backs to read, seconds
ET = datetime.timezone(datetime.timedelta(hours=-5))
SINCE = datetime.datetime(2026, 11, 9, 5, 0, tzinfo=datetime.UTC).timestamp()
s = config.load()
st = db.connect(s.db_path, "main", shared=True)
tape = db.connect(s.tape_path, "tape", shared=True)
rows = st.execute("SELECT ts, ticker, side, contracts, stake_usd, exit_price, outcome FROM decisions WHERE lane='crypto_multiscale' AND mode='trade' AND action='bet' AND stake_usd>0 AND ts>? AND ticker LIKE '%15M-%' ORDER BY ts", (SINCE,)).fetchall()
buys = []
for r in rows:
    t = float(r["ts"]); side = r["side"]; n, cost = float(r["contracts"]), float(r["stake_usd"])
    if r["exit_price"] is not None:
        v = n * (float(r["exit_price"]) - fee_per_contract(float(r["exit_price"]))) - cost
    elif r["outcome"] in ("yes", "no"):
        v = (n if r["outcome"] == side else 0.0) - cost
    else:
        continue
    q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts>? AND ts<=? ORDER BY ts", (r["ticker"], t - 1000.0, t)).fetchall()
    if len(q) < 30 or t - float(q[0][0]) < 300.0:
        continue
    ts = [float(x[0]) for x in q]
    mid = [((float(x[1]) + float(x[2])) / 2.0 if side == "yes" else 1.0 - (float(x[1]) + float(x[2])) / 2.0) for x in q]
    ymid = [(float(x[0]), (float(x[1]) + float(x[2])) / 2.0) for x in q]
    unit = quoteshock.typical_minute(ymid, t, 900.0)
    at = lambda x: mid[max(bisect.bisect_right(ts, x) - 1, 0)]
    res = {}
    for span in SPANS:
        samples = [(t - span + k, at(t - span + k)) for k in range(int(span) + 1)]
        res[("end", span)] = at(t) > at(t - span)
        res[("slope", span)] = curverisk.still_rising(samples, t, span, unit)
    res["both"] = res.get(("end", 180.0), False) and res.get(("slope", 60.0), False)
    res["dip"] = res.get(("end", 180.0), False) and not res.get(("end", 30.0), True)  # up over three minutes, not up over the last 30 s: a dip inside a rise
    buys.append(dict(v=v, cost=cost, res=res))
print(f"live crypto_multiscale buys since Mon 2026-11-09 with quotes and a result: {len(buys)}; net {sum(b['v'] for b in buys):+.2f}")


def line(name, grp):
    if not grp:
        return f"   {name:8s} n=  0"
    return f"   {name:8s} n={len(grp):3d} net {sum(b['v'] for b in grp):+8.2f} per $1 {sum(b['v'] for b in grp) / sum(b['cost'] for b in grp):+.3f} lost {sum(1 for b in grp if b['v'] < 0)}"


for kind, label in (("end", "endpoint: price now above its price span s ago"), ("slope", "slope: rising over the span and within the noise of its high (the exit's reading)")):
    for span in SPANS:
        print(f"\n{label}, span {span:.0f} s")
        print(line("passes", [b for b in buys if b["res"][(kind, span)]])); print(line("fails", [b for b in buys if not b["res"][(kind, span)]]))
if 180.0 in SPANS and 60.0 in SPANS:
    print("\nboth: gaining over 180 s and still rising over the last 60 s")
    print(line("passes", [b for b in buys if b["res"]["both"]])); print(line("fails", [b for b in buys if not b["res"]["both"]]))
if 180.0 in SPANS and 30.0 in SPANS:
    print("\ndip in a rise: above its price 180 s ago but not above its price 30 s ago")
    print(line("passes", [b for b in buys if b["res"]["dip"]])); print(line("fails", [b for b in buys if not b["res"]["dip"]]))
print("DONE")
