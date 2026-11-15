"""Lower the high-bid hold? (2026-11-15, user: "Yes, test it", after the review showed takes sold at 0.80 to 0.95 gave up $46.28 on bets that went on to win, none of the reversals sitting in 0.90 to 0.95.)
The live hold (curverisk.HIGH_BID, 0.95): from that bid up, a flat bid is held at any time left while the spot cushion backs the price (curverisk.flat_holds: the gap between what the sale pays and the curve's chance within the market's swing over the time left); a dip beyond the noise still sells; the strict cushion check (hold while the curve's chance is over what the sale pays) is live as well. Candidates move only the line: 0.95 (live), 0.92, 0.90, 0.88, 0.85 and none (only the last fifth of the market).
Same bets: every live crypto_multiscale bet since the take went live (13:00 ET 2026-11-14), each walked second by second from its buy on the taped quotes (the side's bid; the typical minute re-read every 5 s; the curve's chance from the settlement index with the lane's own read), with the live take (clock bar and late cap, any rise over 8% of the time left within the noise of its high, the flat hold, the strict cushion check) and the price-only max-loss stop (no wait, no hour view). A bet neither sells settles; fee on both legs. Said plainly: an approximation of the lane (its live baseline is printed against the real result), one night, not split by curve shape; the 0.90 line was chosen after seeing the review.
Run on the host: python scripts/high_bid_threshold_replay.py"""
import bisect
import datetime
import json
import urllib.request

from kalshi_moonshot import config, curvevalue, curverisk, db, quoteshock, variants
from kalshi_moonshot.strategy import fee_per_contract

ET = datetime.timezone(datetime.timedelta(hours=-5))
SINCE = datetime.datetime(2026, 11, 14, 13, 0, tzinfo=ET).timestamp()
HIGHS = [0.95, 0.92, 0.90, 0.88, 0.85, 9.0]
s = config.load()
st = db.connect(s.db_path, "main", shared=True)
tape = db.connect(s.tape_path, "tape", shared=True)
rows = st.execute("SELECT ts, ticker, side, contracts, stake_usd, exit_price, exit_ts, outcome, close_time FROM decisions WHERE lane='crypto_multiscale' AND mode='trade' AND action='bet' AND stake_usd>0 AND ts>? ORDER BY ts", (SINCE,)).fetchall()
mk = {}


def market(ticker):
    if ticker not in mk:
        try:
            with urllib.request.urlopen(f"https://api.elections.kalshi.com/trade-api/v2/markets/{ticker}", timeout=10) as f:
                m = json.load(f)["market"]
            mk[ticker] = (m.get("result") if m.get("result") in ("yes", "no") else None, float(m["floor_strike"]) if m.get("floor_strike") else None)
        except Exception:
            mk[ticker] = (None, None)
    return mk[ticker]


def flat_holds(net, chance, unit, left):
    return chance is not None and unit is not None and unit > 0 and left > 0 and net - chance <= unit * (left / 60.0) ** 0.5


data = []
for r in rows:
    close = datetime.datetime.fromisoformat(str(r["close_time"]).replace("Z", "+00:00")).timestamp(); t0 = float(r["ts"])
    res, strike = market(r["ticker"])
    q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts>? AND ts<=? ORDER BY ts", (r["ticker"], t0 - 1000.0, close)).fetchall()
    if res is None or strike is None or len(q) < 30:
        continue
    side = r["side"]; n, cost = float(r["contracts"]), float(r["stake_usd"])
    ts = [float(x[0]) for x in q]; bd = [(float(x[1]) if side == "yes" else 1.0 - float(x[2])) for x in q]
    mids = [(float(x[0]), (float(x[1]) + float(x[2])) / 2.0) for x in q]
    ix = variants.spot_index(r["ticker"].split("-")[0], "")
    g = tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND ts>? AND ts<=? AND value>0 ORDER BY ts", (ix, t0 - 1300.0, close)).fetchall() if ix else []
    real = (n * (float(r["exit_price"]) - fee_per_contract(float(r["exit_price"]))) - cost) if r["exit_price"] is not None else ((n if res == side else 0.0) - cost)
    data.append(dict(t=r["ticker"], side=side, n=n, cost=cost, close=close, t0=t0, res=res, strike=strike, ts=ts, bd=bd, mids=mids, its=[float(x[0]) for x in g], ivs=[float(x[1]) for x in g], real=real, chance={}))


def chance_at(d, t, left):
    k = int(t)
    if k not in d["chance"]:
        jt = bisect.bisect_right(d["its"], t)
        rd = curvevalue.read(d["its"][max(jt - 1100, 0):jt], d["ivs"][max(jt - 1100, 0):jt], t, d["strike"], d["side"], left, "900+180") if jt else None
        d["chance"][k] = rd.p if rd else None
    return d["chance"][k]


def walk(d, high):
    entry = d["cost"] / d["n"]; basis = entry + fee_per_contract(entry); room = 1.0 - basis
    at = lambda x: d["bd"][max(bisect.bisect_right(d["ts"], x) - 1, 0)]
    hist, unit, unit_at, t = [], None, 0.0, d["t0"] + 1.0
    while t < d["close"] - 8.0:
        bid, left = at(t), d["close"] - t; look = 0.08 * left
        hist.append((t, bid))
        while hist and hist[0][0] < t - max(60.0, 2 * look):
            hist.pop(0)
        if t - unit_at >= 5.0:
            unit_at = t; unit = quoteshock.typical_minute(d["mids"][: bisect.bisect_right(d["ts"], t)], t, 900.0)
        net = bid - fee_per_contract(bid); gain = net - basis; pnl = gain / basis
        if gain > 0 and bid >= 0.05 and curverisk.take_profit(gain, room, left, 900.0, fee_per_contract(bid), unit):
            ch = chance_at(d, t, left)
            strict_hold = ch is not None and ch > net
            flat_ok = (left <= 180.0 or bid >= high) and flat_holds(net, ch, unit, left)
            if not strict_hold and not curverisk.still_rising(hist, t, look, unit, flat_ok):
                return d["n"] * net - d["cost"], "take", t, bid
        if curverisk.stopped(pnl, min(max(net, 0.0), 1.0), 0.5):
            return d["n"] * net - d["cost"], "stop", t, bid
        t += 1.0
    return (d["n"] if d["res"] == d["side"] else 0.0) - d["cost"], "settle", None, None


print(f"live bets since the take went live with taped quotes, a strike and a result: {len(data)}; the real run made {sum(d['real'] for d in data):+.2f}")
res = {}
for h in HIGHS:
    rows_ = [walk(d, h) for d in data]; res[h] = rows_
    tk = [r for r in rows_ if r[1] == "take"]; sp = [r for r in rows_ if r[1] == "stop"]; se = [r for r in rows_ if r[1] == "settle"]
    name = "none (last fifth only)" if h > 1 else f"{h:.2f}" + (" (live)" if h == 0.95 else "")
    print(f"hold from {name:24s}: total {sum(r[0] for r in rows_):+8.2f} | takes {len(tk):2d} {sum(r[0] for r in tk):+7.2f} | stops {len(sp):2d} {sum(r[0] for r in sp):+7.2f} | settle {len(se):2d} {sum(r[0] for r in se):+7.2f}")
base = res[0.95]
print("\nchange against the live line (0.95), bets that differ by more than $0.50:")
for h in HIGHS[1:]:
    diffs = [(i, res[h][i][0] - base[i][0]) for i in range(len(data)) if abs(res[h][i][0] - base[i][0]) > 0.5]
    print(f"hold from {('none' if h > 1 else f'{h:.2f}'):5s}: net {sum(res[h][i][0] for i in range(len(data))) - sum(b[0] for b in base):+8.2f}; better on {sum(1 for _, v in diffs if v > 0)}, worse on {sum(1 for _, v in diffs if v < 0)}")
    if h in (0.90, 0.85):
        for i, v in sorted(diffs, key=lambda x: x[1]):
            d = data[i]
            print(f"     {d['t'][2:-3]:20s} {d['side']:3s} {datetime.datetime.fromtimestamp(d['t0'], ET):%H:%M} live {base[i][1]} {base[i][0]:+7.2f} -> {res[h][i][1]} {res[h][i][0]:+7.2f} ({v:+.2f}); ended {d['res']}")
print("DONE")
