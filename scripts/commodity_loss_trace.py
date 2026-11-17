"""Why did the live commodity bets that lost lose, and where on the way down could they have been sold? (2026-11-17, user: "So why did the losses happen on live? What scenario led to the decline we could have
sold on and at what time? There's a single rule or two that should fit".)

A trace, not a rule: every live commodity bet in the window (default: since the drop shadow went on, 2026-11-16 21:02 ET; SINCE=YYYY-MM-DDTHH:MM for another start), followed on Kalshi's own quote for the side
held from the buy to the close. For each bet: what was paid, when, the lowest bid, the first moment the bid was under even (0.50) and how long it then stayed there, the bid 30/60/120 s into that stretch, and
how far the bid fell from its high since the buy in the market's own typical minutes (quoteshock.typical_minute at the buy). Losers get their path every 30 s. Winners get the same columns, so anything the
losers share can be seen against the winners that did the same. Kalshi's quote only; no outside feed.
    scripts/cloud.sh run scripts/commodity_loss_trace.py
"""
import bisect
import sys
import traceback
import datetime
import os

from kalshi_moonshot import config, db, quoteshock
from kalshi_moonshot.strategy import fee_per_contract as fee

ET = datetime.timezone(datetime.timedelta(hours=-5))
SINCE = datetime.datetime.strptime(os.environ.get("SINCE", "2026-11-16T21:02"), "%Y-%m-%dT%H:%M").replace(tzinfo=ET).timestamp()
UNTIL = datetime.datetime.strptime(os.environ["UNTIL"], "%Y-%m-%dT%H:%M").replace(tzinfo=ET).timestamp() if os.environ.get("UNTIL") else 9e18
BRIEF = bool(os.environ.get("BRIEF"))
s = config.load()
st = db.connect(s.db_path, "main", shared=True)
tape = db.connect(s.tape_path, "tape", shared=True)
results = {w["ticker"]: (w["result"], float(w["close_ts"])) for w in st.execute("SELECT ticker, close_ts, result FROM windows WHERE close_ts>? AND result IN ('yes','no')", (SINCE,)).fetchall()}
rows = st.execute("SELECT ts, ticker, lane, side, ask, stake_usd, contracts, exit_price, exit_ts, exit_reason FROM decisions WHERE lane LIKE 'commodity%' AND mode='trade' AND action='bet' AND stake_usd>0 AND ts>? AND ts<? ORDER BY ts", (SINCE, UNTIL)).fetchall()
out = []
for r in rows:
    if r["ticker"] not in results:
        continue
    res, close = results[r["ticker"]]
    t0, side = float(r["ts"]), r["side"]
    q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (r["ticker"], close - 920, close)).fetchall()
    if len(q) < 20:
        continue
    qt = [float(x[0]) for x in q]
    mids = [(float(x[0]), (float(x[1]) + float(x[2])) / 2) for x in q]

    def bid_at(t, q=q, qt=qt, side=side):  # bound now: each bet keeps its own quotes
        i = bisect.bisect_right(qt, t) - 1
        if i < 0 or t - qt[i] > 60:
            return None
        return float(q[i][1]) if side == "yes" else 1.0 - float(q[i][2])

    unit = quoteshock.typical_minute(mids[: bisect.bisect_right(qt, t0)], t0)
    stake, n0, ask = float(r["stake_usd"]), float(r["contracts"] or 0.0), float(r["ask"] or 0.0)
    basis = stake / n0 if n0 > 0 and ask - 0.06 <= stake / n0 <= ask + fee(ask) + 0.06 else ask + fee(ask)
    n = stake / basis
    won = res == side
    sold = r["exit_price"] is not None and r["exit_ts"] is not None and float(r["exit_ts"]) < close - 1
    actual = ((float(r["exit_price"]) - fee(float(r["exit_price"])) if sold else (1.0 if won else 0.0)) - basis) * n
    path = [(t, bid_at(t)) for t in range(int(t0) + 1, int(close) - 1)]
    path = [(t, b) for t, b in path if b is not None]
    if not path:
        continue
    low = min(b for _, b in path)
    peak, worst_fall = 0.0, 0.0
    for _, b in path:
        peak = max(peak, b)
        worst_fall = max(worst_fall, peak - b)
    # stretches under even: the first one, and the longest one that ended with the bid back over even
    first, run, longest_back, cur = None, 0, 0, None
    for t, b in path:
        if b < 0.50:
            if cur is None:
                cur = t
                if first is None:
                    first = t
        elif cur is not None:
            longest_back = max(longest_back, t - cur)
            cur = None
    stay = (path[-1][0] - cur) if cur is not None else 0  # seconds under even at the end without coming back
    out.append({"r": r, "close": close, "t0": t0, "basis": basis, "n": n, "won": won, "sold": sold, "actual": actual, "unit": unit, "low": low, "fall": worst_fall, "first": first,
                "back": longest_back, "stay": stay, "final_from": cur, "bid_at": bid_at, "path": path})


def hm(t):
    return datetime.datetime.fromtimestamp(t, ET).strftime("%m-%d %H:%M:%S")


def row(x):
    r = x["r"]
    u = x["unit"]
    f = x["first"]
    after = "" if f is None else " ".join(f"+{d}s {b:.2f}" if (b := x["bid_at"](f + d)) is not None and f + d < x["close"] else f"+{d}s  -- " for d in (30, 60, 120))
    return (f"{hm(x['t0'])} {r['ticker'][:26]:26s} {r['side']:3s} paid {x['basis']:.2f} {x['close'] - x['t0']:4.0f}s left | low {x['low']:.2f} | fall from high {x['fall']:.2f}"
            f" ({x['fall'] / u:4.1f} units of {u:.3f})" if u else f"{hm(x['t0'])} {r['ticker'][:26]:26s} {r['side']:3s} paid {x['basis']:.2f} | low {x['low']:.2f} | fall {x['fall']:.2f} (no unit)") + (
        " | never under even" if f is None else f" | first under even with {x['close'] - f:3.0f}s left: {after} | longest dip under even that came back {x['back']:3.0f}s | under even to the end for {x['stay']:3.0f}s") + f" | ${x['actual']:+.2f}"


losers = [x for x in out if x["actual"] < 0]
winners = [x for x in out if x["actual"] >= 0]
print(f"window: live commodity bets {hm(SINCE)} ET on; {len(out)} settled with quotes; {len(losers)} lost money (${sum(x['actual'] for x in losers):+.2f}), {len(winners)} did not (${sum(x['actual'] for x in winners):+.2f})")
if not BRIEF:
    print("\n=== the bets that lost money")
    for x in losers:
        print(row(x) + (f" | sold by the lane at {float(x['r']['exit_price']):.2f}: {(x['r']['exit_reason'] or '')[:60]}" if x["sold"] else " | held to zero"))
        pts = [(t, b) for t, b in x["path"] if (t - int(x["t0"])) % 30 == 1]
        print("      every 30 s: " + " ".join(f"{b:.2f}" for _, b in pts))
    print("\n=== the bets that did not lose (same columns)")
    for x in winners:
        if x["first"] is not None or (x["unit"] and x["fall"] / x["unit"] >= 2):
            print(row(x))
    print(f"  ... and {sum(1 for x in winners if x['first'] is None and not (x['unit'] and x['fall'] / x['unit'] >= 2))} winners that were never under even and never fell 2 units from their high")

print("\n=== what the two groups did")
for name, g in (("lost money", losers), ("did not lose", winners)):
    k = len(g)
    under = [x for x in g if x["first"] is not None]
    print(f"  {name:13s} n {k:3d} | ever under even {len(under):3d} | under even 30 s straight {sum(1 for x in g if max(x['back'], x['stay']) >= 30):3d} | 60 s straight {sum(1 for x in g if max(x['back'], x['stay']) >= 60):3d} | 120 s straight {sum(1 for x in g if max(x['back'], x['stay']) >= 120):3d}"
          f" | fell 2+ units from the high {sum(1 for x in g if x['unit'] and x['fall'] / x['unit'] >= 2):3d} | 4+ units {sum(1 for x in g if x['unit'] and x['fall'] / x['unit'] >= 4):3d}")

sys.excepthook = lambda *e: print("".join(traceback.format_exception(*e)))
print("\n=== one stated rule on the same bets: the bid for our side has been under even (0.50) for a tenth of the time left, without a break; sold at the worse of the next two bids")
tot_a = tot_b = 0.0
beat = lost = fired = 0
for x in out:
    since, sale = None, None
    end = float(x["r"]["exit_ts"]) if x["sold"] else x["close"]
    for t, b in x["path"]:
        if t >= end - 1:
            break
        left = x["close"] - t
        if b < 0.50:
            since = since if since is not None else t
            if t - since >= 0.10 * left and b >= 0.05:
                fills = [v for v in (x["bid_at"](t + 1), x["bid_at"](t + 2)) if v is not None]
                fill = min(fills) if fills else b
                sale = ((fill - fee(fill) - x["basis"]) * x["n"], fill, left)
                break
        else:
            since = None
    tot_a += x["actual"]
    tot_b += sale[0] if sale else x["actual"]
    if sale:
        fired += 1
        beat += sale[0] > x["actual"] + 1e-9
        lost += sale[0] < x["actual"] - 1e-9
        if not BRIEF:
            print(f"    {hm(x['t0'])} {x['r']['ticker'][:26]:26s} sells {sale[1]:.2f} with {sale[2]:3.0f}s left ${sale[0]:+.2f} | actual ${x['actual']:+.2f}")
print(f"  n {len(out)} | what happened ${tot_a:+.2f} | with the rule ${tot_b:+.2f} | change ${tot_b - tot_a:+.2f} | rule sold {fired}: beat {beat}, lost to {lost}")
print("DONE")
