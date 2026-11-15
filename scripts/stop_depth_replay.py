"""The stop's depth against the take's size (2026-11-15, user: "Yes, run it", after tonight's two whipsaw hours: takes earn about +15% and stops lose 45% to 76%).
Live: the max-loss stop sells once the loss reaches max_loss x the chance the side pays (curverisk.stopped: pnl <= -0.5 x the net bid), after a wait. Candidates move only that depth: 0.25, 0.35 (shallower, sells sooner), 0.5 (live), 0.7 and 1.0 (deeper).
Same bets: every live crypto_multiscale bet since the take went live (13:00 ET on 2026-11-14), each walked second by second from its buy on the taped quotes (the side's bid; the typical minute re-read every 5 s), with the live take (clock bar and late cap, any rise over 8% of the time left, within the noise of its high) and the price-only stop: no wait, no hour view, no squeeze, no cushion check, so the walk is an approximation of the lane (its live baseline is printed against the real result). A bet neither sells settles; fee on both legs. Said plainly: the replay does not walk the stop's wait or the hour view, both of which make the live stop slower than this one; not split by curve shape.
Run on the host: python scripts/stop_depth_replay.py"""
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


DEPTHS = [0.25, 0.35, 0.5, 0.7, 1.0]  # max_loss: the loss allowed as a share of the chance (0.5 live)
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


def walk(rec, max_loss):
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
        if gain > 0 and bid >= 0.05 and gain > fee_per_contract(bid) and gain >= bar_of("live", gain, room, left, unit) and not curverisk.still_rising(hist, t, look, unit):
            return n * net - cost, "take", t
        if curverisk.stopped(pnl, min(max(net, 0.0), 1.0), max_loss):
            return n * net - cost, "stop", t
        t += 1.0
    return (n if res == side else 0.0) - cost, "settle", None


print(f"live bets since the take went live with taped quotes and a result: {len(data)}; the real run made {sum(d[9] for d in data):+.2f}")
res = {}
for dpt in DEPTHS:
    rows = [walk(d, dpt) for d in data]
    res[dpt] = rows
    takes = [r for r in rows if r[1] == "take"]; stops = [r for r in rows if r[1] == "stop"]; settles = [r for r in rows if r[1] == "settle"]
    print(f"max_loss {dpt:4.2f}{' (live)' if dpt == 0.5 else '       '}: total {sum(r[0] for r in rows):+8.2f} | takes {len(takes):2d} avg {sum(r[0] for r in takes)/max(len(takes),1):+6.2f} | stops {len(stops):2d} avg {sum(r[0] for r in stops)/max(len(stops),1):+6.2f} | settle {len(settles):2d} avg {sum(r[0] for r in settles)/max(len(settles),1):+6.2f}")
base = res[0.5]
print("\nchange against the live depth (bets that differ by more than $0.50):")
for dpt in DEPTHS:
    if dpt == 0.5:
        continue
    better = sum(1 for i in range(len(data)) if res[dpt][i][0] > base[i][0] + 0.5)
    worse = sum(1 for i in range(len(data)) if res[dpt][i][0] < base[i][0] - 0.5)
    print(f"max_loss {dpt:4.2f}: net {sum(r[0] for r in res[dpt]) - sum(r[0] for r in base):+8.2f}, better on {better} bets, worse on {worse}")
print("DONE")
