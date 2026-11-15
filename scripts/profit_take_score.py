"""Score every live profit take against what holding the same bet to settlement paid.

One question, same bets (CLAUDE.md rule 4): each bet the curve risk exit sold as a "profit take", its realized
result after the sale fee against the settlement result of the same contracts. Split by which bar sold it: the late
cap ("half the room") or the time bar, and by whether the late cap was the only reason (the bet's share of its room
was under the share of the market gone, so the time bar alone would have held).

Run on the host: LANE=crypto_multiscale python scripts/profit_take_score.py
"""
import bisect
import datetime
import json
import math
import os
import re
import urllib.request
from collections import defaultdict
from statistics import NormalDist

from kalshi_moonshot import config, db
from kalshi_moonshot import curvevalue, variants
from kalshi_moonshot.commodities import instrument_for, load_instruments
from kalshi_moonshot.strategy import fee_per_contract

LANE = os.environ.get("LANE", "crypto_multiscale")
SINCE = datetime.datetime(2026, 11, 9, 5, 0, tzinfo=datetime.UTC).timestamp()  # Monday 2026-11-09 00:00 ET
s = config.load()
st = db.connect(s.db_path, "main", shared=True)
rows = st.execute(
    "SELECT id, ts, ticker, side, stake_usd, exit_price, exit_ts, exit_reason, outcome, close_time, mode FROM decisions "
    "WHERE lane=? AND ts>=? AND action='bet' AND stake_usd>0 AND exit_reason LIKE ? ORDER BY ts",
    (LANE, SINCE, "curve risk exit (profit take)%"),
).fetchall()
orders = defaultdict(list)
for ticker, ts, side, count, fills, cost, fee, contract in st.execute(
    "SELECT ticker, ts, side, count, fill_count, cost_usd, fee_usd, contract FROM orders WHERE ts>=?", (SINCE,)
):
    orders[ticker].append((float(ts), side, float(fills or 0), float(cost or 0), float(fee or 0), contract))

N = NormalDist()
tape = db.connect(s.tape_path, "tape", shared=True)
insts = load_instruments(s.data_dir)


def strike_of(ticker):
    try:
        with urllib.request.urlopen(f"https://api.elections.kalshi.com/trade-api/v2/markets/{ticker}", timeout=10) as f:
            return float(json.load(f)["market"]["floor_strike"])
    except Exception:
        return None


def cushion_z(ticker, side, at, left):
    """(z, the lane's curve chance). The spot's distance to the strike on the side we hold, in the market's own variance over the time left: the rms of the index's one-minute moves over the hour before `at`, times sqrt(left). None when the tape or strike is missing."""
    inst = instrument_for(ticker.split("-")[0], insts)
    strike = strike_of(ticker)
    if inst is None or strike is None:
        return None, None
    ix = variants.spot_index(ticker.split("-")[0], "") or inst.symbol.removeprefix("kp:")  # the index the lane's own exit reads (a coin's Kalshi settlement index), else the perp
    got = tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND ts>? AND ts<=? AND value>0 ORDER BY ts", (ix, at - 3700.0, at)).fetchall()
    if len(got) < 100:
        return None, None
    ts, vs = [float(r[0]) for r in got], [float(r[1]) for r in got]
    lv = []
    for k in range(61):
        i = bisect.bisect_right(ts, at - 60.0 * k) - 1
        if i < 0:
            break
        lv.append(math.log(vs[i]))
    mv = [lv[k] - lv[k + 1] for k in range(len(lv) - 1)]
    if len(mv) < 30:
        return None, None
    rate = math.sqrt(sum(m * m for m in mv) / len(mv) / 60.0)
    sign = 1.0 if side == "yes" else -1.0
    read = curvevalue.read(ts, vs, at, strike, side, left, "900+180")  # the lane's own curve read: the lower chance of the 900 s and 180 s rates
    return sign * math.log(vs[-1] / strike) / (rate * math.sqrt(max(left, 1.0))), (read.p if read is not None else None)


rows_out = []
groups = defaultdict(lambda: [0, 0.0, 0.0, 0, 0, 0])  # n, realized, held, sale beat holding, holding beat sale, unsettled
print(f"lane {LANE}: profit takes since Mon 2026-11-09 00:00 ET: {len(rows)}")
for id_, ts, ticker, side, stake, exit_price, exit_ts, why, outcome, close_time, mode in rows:
    close = datetime.datetime.fromisoformat(str(close_time).replace("Z", "+00:00")).timestamp()
    left = close - float(exit_ts)
    gone = 1.0 - left / 900.0
    m = re.search(r"(\d+)% of its ([\d.]+) room against a bar of (\d+)%", why or "")
    share = float(m.group(1)) / 100 if m else None
    capped = "half the room" in (why or "")
    kind = "late cap only (time bar would have held)" if capped and share is not None and share < gone else "late cap, time bar also met" if capped else "time bar"
    # every fill of the bet: buys of this side from the decision to the sale, sells of it around the sale
    buy = [o for o in orders[ticker] if o[1] == "buy" and o[5] == side and float(ts) - 30 <= o[0] <= float(exit_ts)]
    sell = [o for o in orders[ticker] if o[1] == "sell" and o[5] == side and float(exit_ts) - 30 <= o[0] <= float(exit_ts) + 90]
    if buy and sell:
        n = sum(o[2] for o in sell)
        bought = sum(o[2] for o in buy)
        cost = sum(o[3] + o[4] for o in buy) * (n / bought if bought else 1.0)  # the cost of the contracts that were sold
        got = sum(-o[3] - o[4] for o in sell)
        fill = sum(-o[3] for o in sell) / n if n else 0.0
    else:  # paper, or the order rows are missing
        n, cost, got, fill = None, float(stake), None, float(exit_price)
    realized = (got - cost) if got is not None else None
    if outcome in ("yes", "no") and n is not None:
        held = (n if outcome == side else 0.0) - cost
    else:
        held = None
    g = groups[kind]
    if realized is None or held is None:
        g[5] += 1
    else:
        g[0] += 1
        g[1] += realized
        g[2] += held
        g[3] += realized > held
        g[4] += held > realized
    z, p_curve = cushion_z(ticker, side, float(exit_ts), left)
    net_bid = float(exit_price) - fee_per_contract(float(exit_price))
    rows_out.append((ticker, kind, z, p_curve, net_bid, realized, held))
    when = datetime.datetime.fromtimestamp(float(exit_ts), datetime.timezone(datetime.timedelta(hours=-5))).strftime("%H:%M:%S")
    print(f"  {ticker:28s} {side:3s} sold {when} ET, {left:4.0f}s left, decided {float(exit_price):.2f} filled {fill:.3f}, share {share}, market gone {gone:.0%}: "
          f"realized {realized if realized is None else round(realized, 2)} held {held if held is None else round(held, 2)} outcome {outcome} [{kind}]")
print()
for kind, (n, realized, held, won, lost, open_) in groups.items():
    print(f"{kind}: n={n} realized ${realized:+.2f}, holding ${held:+.2f}, net change from selling ${realized - held:+.2f}; sale beat holding on {won}, holding beat the sale on {lost}; not settled or no order rows: {open_}")
print()
print("cushion at the sale: spot to strike on the side held, in typical-hour units over the time left (z), the curve's chance Phi(z), and the net bid it sold at")
for ticker, kind, z, pc, nb, realized, held in rows_out:
    print(f"  {ticker:28s} z {z if z is None else round(z, 2)}  curve chance {pc if pc is None else round(pc, 3)}  net bid {nb:.3f}  sale vs hold {None if held is None or realized is None else round(realized - held, 2)}")
for name, hold in (("hold when the curve chance is over the net bid", lambda pc, nb: pc > nb), ("hold when the curve chance is over the net bid by 0.05", lambda pc, nb: pc > nb + 0.05)):
    scored = [(r, h) for _, _, z, pc, nb, r, h in rows_out if pc is not None and r is not None and h is not None]
    kept = [(r, h) for (_, _, z, pc, nb, r, h) in rows_out if pc is not None and r is not None and h is not None and not hold(pc, nb)]
    held_back = [(r, h) for (_, _, z, pc, nb, r, h) in rows_out if pc is not None and r is not None and h is not None and hold(pc, nb)]
    print(f"rule '{name}': of {len(scored)} settled takes it would still sell {len(kept)} (sale vs hold {sum(r - h for r, h in kept):+.2f}) and hold {len(held_back)} (what holding paid over the sale {sum(h - r for r, h in held_back):+.2f})")
print("DONE")
