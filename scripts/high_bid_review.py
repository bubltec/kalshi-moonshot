"""Sure wins and the 0.95 rule (2026-11-15, user: "still not holding on to sure wins"; "our 95c rule - did it help?").
Every live crypto_multiscale bet bought since the 0.95 hold went live (00:16 ET 2026-11-15). A: each bet's entry, highest bid after the buy, how it ended and what a take gave up against settlement. B: each take sold at a bid of 0.95 or over, and why the hold did not stop it (the cushion's gap against the market's swing, and the bid's dip against its noise), read from the sale's own reason and the taped bids. C: each bet NOT sold by a take that had a bid of 0.95 or over after the buy: the first second the take as it was before the 0.95 hold (the strict cushion check only) would have sold it, and what that sale would have made against what the bet really got. Fee on both legs. Said plainly: C reads the spot cushion from the settlement index with the lane's own curve read, the take bar and the any-rise slope as the live code does, and is an approximation.
Run on the host: python scripts/high_bid_review.py"""
import bisect
import datetime
import json
import math
import re
import urllib.request

from kalshi_moonshot import config, curvevalue, curverisk, db, quoteshock, variants
from kalshi_moonshot.strategy import fee_per_contract

ET = datetime.timezone(datetime.timedelta(hours=-5))
SINCE = datetime.datetime(2026, 11, 15, 0, 16, tzinfo=ET).timestamp()
s = config.load()
st = db.connect(s.db_path, "main", shared=True)
tape = db.connect(s.tape_path, "tape", shared=True)
rows = st.execute("SELECT ts, ticker, side, contracts, stake_usd, exit_price, exit_ts, exit_reason, outcome, close_time FROM decisions WHERE lane='crypto_multiscale' AND mode='trade' AND action='bet' AND stake_usd>0 AND ts>? ORDER BY ts", (SINCE,)).fetchall()
cache = {}


def market(ticker):
    if ticker not in cache:
        try:
            with urllib.request.urlopen(f"https://api.elections.kalshi.com/trade-api/v2/markets/{ticker}", timeout=10) as f:
                m = json.load(f)["market"]
            cache[ticker] = (m.get("result") if m.get("result") in ("yes", "no") else None, float(m["floor_strike"]) if m.get("floor_strike") else None)
        except Exception:
            cache[ticker] = (None, None)
    return cache[ticker]


f = lambda t: datetime.datetime.fromtimestamp(float(t), ET).strftime("%H:%M:%S")
data = []
for r in rows:
    n = float(r["contracts"]); cost = float(r["stake_usd"]); side = r["side"]; entry = cost / n
    close = datetime.datetime.fromisoformat(str(r["close_time"]).replace("Z", "+00:00")).timestamp()
    t0 = float(r["ts"]); end = float(r["exit_ts"]) if r["exit_ts"] is not None else close
    res, strike = market(r["ticker"])
    q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts>? AND ts<=? ORDER BY ts", (r["ticker"], t0 - 1000.0, close)).fetchall()
    if len(q) < 30 or res is None:
        continue
    ts = [float(x[0]) for x in q]; bd = [(float(x[1]) if side == "yes" else 1.0 - float(x[2])) for x in q]
    mids = [(float(x[0]), (float(x[1]) + float(x[2])) / 2.0) for x in q]
    hold_val = (n if res == side else 0.0) - cost
    if r["exit_price"] is not None:
        x = float(r["exit_price"]); sold_val = n * (x - fee_per_contract(x)) - cost
        kind = "take" if "profit take" in (r["exit_reason"] or "") else "stop" if "max loss" in (r["exit_reason"] or "") else "other"
    else:
        x, sold_val, kind = None, hold_val, "held"
    after = [b for t, b in zip(ts, bd) if t0 <= t <= end]
    data.append(dict(r=r, n=n, cost=cost, side=side, entry=entry, close=close, t0=t0, end=end, res=res, strike=strike, ts=ts, bd=bd, mids=mids, hold=hold_val, real=sold_val, kind=kind, x=x, high=max(after) if after else None))

print("A. every live bet since 00:16 ET (entry, highest bid while held, how it ended, real result, what holding to settlement would have made)")
for d in data:
    print(f"  {f(d['t0'])} {d['r']['ticker'][2:-3]:20s} {d['side']:3s} {d['entry']:.2f} high {d['high']:.2f} {d['kind']:5s}" + (f" @{d['x']:.2f}" if d['x'] else "      ") + f" real {d['real']:+7.2f} hold {d['hold']:+7.2f} -> ended {d['res']}")
takes = [d for d in data if d["kind"] == "take" and d["res"] == d["side"]]
gave = sum(d["hold"] - d["real"] for d in takes)
print(f"\ntakes on bets that then won: {len(takes)}, real {sum(d['real'] for d in takes):+.2f}, holding would have made {sum(d['hold'] for d in takes):+.2f}: given up {gave:+.2f}")
for lo, hi in ((0, 0.80), (0.80, 0.90), (0.90, 0.95), (0.95, 1.01)):
    g = [d for d in takes if lo <= d["x"] < hi]
    if g:
        print(f"  sold at {lo:.2f} to {hi:.2f}: {len(g)} takes, gave up {sum(d['hold'] - d['real'] for d in g):+.2f} (avg {sum(d['hold'] - d['real'] for d in g)/len(g):+.2f})")
print(f"takes on bets that then LOST (the take saved money): {sum(1 for d in data if d['kind']=='take' and d['res'] != d['side'])}")

print("\nB. takes sold at a bid of 0.95 or over: why the hold did not stop them")
for d in data:
    if d["kind"] != "take" or d["x"] < 0.949:
        continue
    why = d["r"]["exit_reason"] or ""
    m = re.search(r"typical minute ([0-9.]+)", why); c = re.search(r"chance ([0-9.]+) is not over the ([0-9.]+) the sale pays", why)
    left = d["close"] - d["end"]
    unit = float(m.group(1)) if m else None
    look = 0.08 * left
    w = [b for t, b in zip(d["ts"], d["bd"]) if d["end"] - look <= t <= d["end"]]
    dip = (max(w) - w[-1]) if w else None
    noise = unit * math.sqrt(look / 60.0) if unit else None
    if c and unit:
        gap = float(c.group(2)) - float(c.group(1)); swing = unit * math.sqrt(left / 60.0)
        cush = f"gap {gap:+.3f} vs swing {swing:.3f} -> {'cushion backs the price' if gap <= swing else 'THIN cushion'}"
    else:
        cush = "no cushion note (the reason was cut or the sale pre-dates the note)"
    dp = f"dip {dip:.3f} vs noise {noise:.3f} -> {'dipped beyond noise' if noise is not None and dip is not None and dip >= noise else 'within noise'}" if dip is not None and noise else "dip n/a"
    print(f"  {f(d['end'])} {d['r']['ticker'][2:-3]:20s} sold @{d['x']:.2f}, {left:.0f}s left, ended {d['res']}, gave up {d['hold'] - d['real']:+.2f} | {cush} | {dp}")

print("\nC. bets not sold by a take that had a bid of 0.95 or over after the buy: when the take, as it was before the 0.95 hold, would have sold")
for d in data:
    if d["kind"] == "take" or d["high"] is None or d["high"] < 0.949 or d["strike"] is None:
        continue
    ix = variants.spot_index(d["r"]["ticker"].split("-")[0], "")
    g = tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND ts>? AND ts<=? AND value>0 ORDER BY ts", (ix, d["t0"] - 1300.0, d["close"])).fetchall() if ix else []
    its = [float(x[0]) for x in g]; ivs = [float(x[1]) for x in g]
    entry = d["entry"]; basis = entry + fee_per_contract(entry); room = 1.0 - basis
    at = lambda x: d["bd"][max(bisect.bisect_right(d["ts"], x) - 1, 0)]
    hist, unit, unit_at, fired = [], None, 0.0, None
    t = d["t0"] + 1.0
    while t < d["end"] - 8.0:
        bid, left = at(t), d["close"] - t; look = 0.08 * left
        hist.append((t, bid))
        while hist and hist[0][0] < t - max(60.0, 2 * look):
            hist.pop(0)
        if t - unit_at >= 5.0:
            unit_at = t; unit = quoteshock.typical_minute(d["mids"][: bisect.bisect_right(d["ts"], t)], t, 900.0)
        net = bid - fee_per_contract(bid); gain = net - basis
        if bid >= 0.95 and gain > 0 and curverisk.take_profit(gain, room, left, 900.0, fee_per_contract(bid), unit) and not curverisk.still_rising(hist, t, look, unit, left <= 180):
            jt = bisect.bisect_right(its, t)
            rd = curvevalue.read(its[max(jt - 1100, 0):jt], ivs[max(jt - 1100, 0):jt], t, d["strike"], d["side"], left, "900+180") if jt else None
            if rd is None or rd.p <= net:
                fired = (t, bid, net, rd.p if rd else None); break
        t += 1.0
    if fired:
        v = d["n"] * fired[2] - d["cost"]
        print(f"  {d['r']['ticker'][2:-3]:20s} {d['side']:3s} bought {entry:.2f}: the old take would have sold at {f(fired[0])} @{fired[1]:.2f} (chance {fired[3] if fired[3] is None else round(fired[3], 3)} vs {fired[2]:.3f}) for {v:+.2f}; held it ended {d['res']} and made {d['hold']:+.2f} -> the hold {'made' if d['hold'] >= v else 'lost'} {abs(d['hold'] - v):.2f}")
    else:
        print(f"  {d['r']['ticker'][2:-3]:20s} {d['side']:3s} bought {entry:.2f}: high {d['high']:.2f}, the old take would not have sold it either; {d['kind']} -> {d['real']:+.2f}")
print("DONE")
