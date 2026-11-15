"""The shape of the profit take's bar across the market (2026-11-14 evening, user: "there's no issue with taking profit early, my issues before were taking profit late in the market"; HYPE 22:45 YES was up 0.12 (28% of its room, bought 7 minutes in) several times and was never sold, then stopped for -65%).
Live: bar = (share of the market gone) x room, and once what is left to win is inside the market's swing (always, in practice) the bar is capped at half the room: so the effective bar rises to 0.5 of the room at the half way point and stays there for the whole second half. Candidates change only that curve, nothing else: (a) live; (b) the clock bar squared (low early, high late) with no cap; (c) the clock bar to the power 1.5 with no cap; (d) the clock bar with no cap (linear all the way).
Same bets: every live crypto_multiscale bet since the take went live. Each is walked second by second from its buy on the taped quotes (the side's bid; the typical minute re-read every 5 s): the first second the take fires under the variant (in profit, bid at or over 0.05, gain over the buy-back fee and the bar, and the bid not still rising: any rise over 8% of the time left, within the noise of its high) is the sale; a bet it does not sell is stopped by the price-only max-loss stop (pnl <= -0.5 x the chance, no wait) or settles; fee on both legs. The live variant is walked too, as the yardstick for the replay (the real results are printed beside it). The cushion check and the flat hold of the last fifth are not walked. Settings chosen after seeing HYPE; said to be so.
Run on the host: python scripts/take_bar_shape_replay.py"""
import bisect
import datetime
import json
import math
import urllib.request

from kalshi_moonshot import config, curverisk, db, quoteshock
from kalshi_moonshot.strategy import fee_per_contract

SINCE = datetime.datetime(2026, 11, 14, 18, 0, tzinfo=datetime.UTC).timestamp()
s = config.load()
st = db.connect(s.db_path, "main", shared=True)
tape = db.connect(s.tape_path, "tape", shared=True)
bets = st.execute("SELECT ts, ticker, side, contracts, stake_usd, exit_price, exit_ts, outcome, close_time FROM decisions WHERE lane='crypto_multiscale' AND mode='trade' AND action='bet' AND stake_usd>0 AND ts>? ORDER BY ts", (SINCE,)).fetchall()


def result_of(ticker, outcome):
    if outcome in ("yes", "no"):
        return outcome
    try:
        with urllib.request.urlopen(f"https://api.elections.kalshi.com/trade-api/v2/markets/{ticker}", timeout=10) as f:
            r = json.load(f)["market"].get("result")
        return r if r in ("yes", "no") else None
    except Exception:
        return None


def bar_of(variant, gain, room, left, unit):
    used = min(max(1.0 - left / 900.0, 0.0), 1.0)
    if variant == "live":
        return curverisk.take_bar(gain, room, left, 900.0, unit)
    power = {"squared": 2.0, "1.5": 1.5, "linear": 1.0}[variant]
    return (used ** power) * room


VARIANTS = ["live", "squared", "1.5", "linear"]
data = []
for b in bets:
    close = datetime.datetime.fromisoformat(str(b["close_time"]).replace("Z", "+00:00")).timestamp(); t0 = float(b["ts"])
    res = result_of(b["ticker"], b["outcome"])
    q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts>? AND ts<=? ORDER BY ts", (b["ticker"], t0 - 1000.0, close)).fetchall()
    if res is None or len(q) < 30:
        continue
    side = b["side"]
    ts = [float(x[0]) for x in q]; bd = [(float(x[1]) if side == "yes" else 1.0 - float(x[2])) for x in q]
    mids = [(float(x[0]), (float(x[1]) + float(x[2])) / 2.0) for x in q]
    n, cost = float(b["contracts"]), float(b["stake_usd"])
    if b["exit_price"] is not None:
        real = n * (float(b["exit_price"]) - fee_per_contract(float(b["exit_price"]))) - cost
    else:
        real = (n if res == side else 0.0) - cost
    data.append((b, close, t0, res, ts, bd, mids, n, cost, real))


def walk(rec, variant):
    b, close, t0, res, ts, bd, mids, n, cost, real = rec
    side = b["side"]; entry = cost / n; basis = entry + fee_per_contract(entry); room = 1.0 - basis
    at = lambda x: bd[max(bisect.bisect_right(ts, x) - 1, 0)]
    hist, unit, unit_at, t = [], None, 0.0, t0 + 1.0
    while t < close - 8.0:
        bid, left = at(t), close - t; look = 0.08 * left
        hist.append((t, bid))
        while hist and hist[0][0] < t - max(60.0, 2 * look):
            hist.pop(0)
        if t - unit_at >= 5.0:
            unit_at = t; unit = quoteshock.typical_minute(mids[: bisect.bisect_right(ts, t)], t, 900.0)
        net = bid - fee_per_contract(bid); gain = net - basis; pnl = gain / basis
        if gain > 0 and bid >= 0.05 and gain > fee_per_contract(bid) and gain >= bar_of(variant, gain, room, left, unit) and not curverisk.still_rising(hist, t, look, unit):
            return n * net - cost, "take", t
        if curverisk.stopped(pnl, min(max(net, 0.0), 1.0), 0.5):
            return n * net - cost, "stop", t
        t += 1.0
    return (n if res == side else 0.0) - cost, "settle", None


print(f"live bets since the take went live with taped quotes and a result: {len(data)}; the real run made {sum(d[9] for d in data):+.2f}")
res = {}
for v in VARIANTS:
    rows = [walk(d, v) for d in data]
    res[v] = rows
    takes = [r for r in rows if r[1] == "take"]
    print(f"{v:8s}: total {sum(r[0] for r in rows):+8.2f} | takes {len(takes):2d} stops {sum(1 for r in rows if r[1]=='stop'):2d} settles {sum(1 for r in rows if r[1]=='settle'):2d}")
print("\nchange against the live bar, per bet (only bets that differ by more than 0.50):")
for i, d in enumerate(data):
    base = res["live"][i]
    diffs = [(v, res[v][i]) for v in VARIANTS[1:] if abs(res[v][i][0] - base[0]) > 0.5]
    if diffs:
        print(f"  {d[0]['ticker'][2:-3]:22s} {d[0]['side']:3s} live {base[1]} {base[0]:+7.2f} (real {d[9]:+7.2f}) | " + " | ".join(f"{v} {r[1]} {r[0]-base[0]:+.2f}" for v, r in diffs))
for v in VARIANTS[1:]:
    better = sum(1 for i in range(len(data)) if res[v][i][0] > res["live"][i][0] + 0.5)
    worse = sum(1 for i in range(len(data)) if res[v][i][0] < res["live"][i][0] - 0.5)
    print(f"{v}: better on {better} bets, worse on {worse}, net {sum(r[0] for r in res[v]) - sum(r[0] for r in res['live']):+.2f}")
print("DONE")
