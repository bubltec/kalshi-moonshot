"""Would the profit take that crypto_multiscale runs have helped the live commodity bets? (2026-11-16, user: "we should consider turning the profit exit we have on crypto_multiscale on the commodity lanes")

One question, same bets (CLAUDE.md rule 4): every live buy of the five commodity lanes since 2026-11-14 17:02 ET (the window the crypto take has been scored on), against what actually happened to it (the lane's own sale, or settlement). One stated rule, the crypto lane's settings as they are live, nothing fitted: curverisk.take_profit (the gain reaches the clock's share of the bet's room, and the fee to buy back), not while the bid is still_rising over 8% of the time left, an early 0.95 in the first 360 s is cashed at once, and a sure win (0.95, or 0.90 in the last fifth) is latched and held.
What is NOT replayed: the cushion check (it can only hold a take back, so this replay sells at least as often as the live rule would), the flat-bid hold in the last fifth, and any buy-back after a take (the commodity lanes do not buy back a side they sold). A take decided at second t fills at the worse of the bids at t+1 and t+2. The take stops at the lane's own sale if that came first. The fee is on every leg.
Shapes: where the side's Kalshi mid sat in its own range since the open at the buy (variants.quote_wave_features), as in entry_selection_study.py. No commodity is named.

Run on the host: scripts/cloud.sh run scripts/commodity_take_replay.py
"""
import bisect
import datetime
import math

from kalshi_moonshot import config, curverisk, db, quoteshock, variants
from kalshi_moonshot.strategy import fee_per_contract as fee

ET = datetime.timezone(datetime.timedelta(hours=-5))
SINCE = datetime.datetime(2026, 11, 14, 17, 2, tzinfo=ET).timestamp()
LANES = ("commodity_energy", "commodity_wti", "commodity_natgas", "commodity_precious", "commodity_industrial")
SLOPE, SURE, SURE_LATE, EARLY_BID, EARLY_S = 0.08, 0.95, 0.90, 0.95, 360.0  # crypto_multiscale's live settings
s = config.load()
st = db.connect(s.db_path, "main", shared=True)
tape = db.connect(s.tape_path, "tape", shared=True)

marks = ",".join("?" for _ in LANES)
rows = st.execute(f"SELECT ts, ticker, side, ask, stake_usd, exit_price, exit_ts, exit_reason FROM decisions WHERE lane IN ({marks}) AND mode='trade' AND action='bet' AND stake_usd>0 AND ts>? ORDER BY ts", (*LANES, SINCE)).fetchall()
bets = {}
for r in rows:
    bets.setdefault((r["ticker"], r["side"]), r)
results = {w["ticker"]: (w["result"], float(w["close_ts"])) for w in st.execute("SELECT ticker, close_ts, result FROM windows WHERE close_ts>? AND result IN ('yes','no')", (SINCE,)).fetchall()}

out = []  # (dollars actual, dollars with the take, taken, sale bid, seconds left at the sale, won, shape, sold by the lane)
skipped = 0
for (ticker, side), r in bets.items():
    if ticker not in results:
        continue
    res, close = results[ticker]
    opened, ts0 = close - 900.0, float(r["ts"])
    q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? ORDER BY ts", (ticker, opened - 20, close)).fetchall()
    q = [(float(x[0]), float(x[1] or 0.0), float(x[2] or 0.0)) for x in q if x[2] and float(x[2]) > 0 and not (float(x[1] or 0.0) <= 0 and float(x[2]) >= 1)]
    qt = [x[0] for x in q]
    mids = [(x[0], (x[1] + x[2]) / 2) for x in q]

    def bid_at(t):
        i = bisect.bisect_right(qt, t) - 1
        if i < 0:
            return None
        return q[i][1] if side == "yes" else 1.0 - q[i][2]

    entry = float(r["ask"] or 0.0)
    if not (0.05 < entry < 0.99):
        i = bisect.bisect_right(qt, ts0) - 1
        entry = (q[i][2] if side == "yes" else 1.0 - q[i][1]) if i >= 0 else 0.0
    if not (0.05 < entry < 0.99) or not q:
        skipped += 1
        continue
    basis = entry + fee(entry)
    won = res == side
    sold = r["exit_price"] is not None and r["exit_ts"] is not None and float(r["exit_ts"]) < close - 1
    actual = (float(r["exit_price"]) - fee(float(r["exit_price"])) if sold else (1.0 if won else 0.0)) - basis
    stop = float(r["exit_ts"]) if sold else close
    hist, latched, level, unit, take = [], False, 0.0, None, None
    for t in range(int(ts0) + 1, int(stop)):
        bid = bid_at(t)
        if bid is None:
            continue
        left = close - t
        if (t - int(ts0)) % 5 == 1:
            k = bisect.bisect_right(qt, t)
            unit = quoteshock.typical_minute(mids[:k], float(t))
        look = SLOPE * left
        hist.append((float(t), bid))
        while hist and hist[0][0] < t - max(60.0, 2.0 * look):
            hist.pop(0)
        net = bid - fee(bid)
        gain, room = net - basis, 1.0 - basis
        wants = gain > 0 and bid >= 0.05 and curverisk.take_profit(gain, room, left, 900.0, fee(bid), unit)
        taken = wants and not curverisk.still_rising(hist, float(t), look, unit, False)
        early = gain > 0 and bid >= EARLY_BID and 900.0 - left <= EARLY_S
        if early:
            taken = True
        if not early and curverisk.sure_win(bid, left, SURE, SURE_LATE):
            latched, level = True, (SURE if bid >= SURE else SURE_LATE)
        elif latched and unit is not None and unit > 0 and bid < level - unit * math.sqrt(left / 60.0):
            latched = False
        if taken and not early and latched:
            taken = False
        if taken:
            fills = [b for b in (bid_at(t + 1), bid_at(t + 2)) if b is not None]
            fill = min(fills) if fills else bid
            take = (fill - fee(fill) - basis, fill, left)
            break
    n = float(r["stake_usd"]) / basis
    sh = variants.quote_wave_features(mids, side, opened, ts0)
    shape = "no reading" if sh is None else "top fifth of its range" if sh["pos"] >= 0.8 else "lower in its range"
    out.append((actual * n, (take[0] if take else actual) * n, take is not None, take[1] if take else None, take[2] if take else None, won, shape, sold))


def line(name, rs):
    if not rs:
        print(f"  {name:40s} n   0")
        return
    a, b = sum(r[0] for r in rs), sum(r[1] for r in rs)
    tk = [r for r in rs if r[2]]
    beat, lost = sum(1 for r in tk if r[1] > r[0] + 1e-9), sum(1 for r in tk if r[1] < r[0] - 1e-9)
    print(f"  {name:40s} n {len(rs):3d} | won {sum(1 for r in rs if r[5]) / len(rs):4.0%} | what happened ${a:+8.2f} | with the take ${b:+8.2f} | change ${b - a:+8.2f} | takes {len(tk):3d}: beat {beat}, lost to {lost}")


print(f"window: since 2026-11-14 17:02 ET; {len(out)} settled live commodity buys with quotes (of {len(bets)} placed; {skipped} without a usable price); {sum(1 for r in out if r[7])} were sold by the lane's own exit")
print("\n1. all bets: what happened against the same bets with the profit take added")
line("all", out)
print("\n2. by the side's own curve at the buy")
for name in ("top fifth of its range", "lower in its range", "no reading"):
    line(name, [r for r in out if r[6] == name])
print("\n3. by how the bet ended")
line("bets that won", [r for r in out if r[5]])
line("bets that lost", [r for r in out if not r[5]])
print("\n4. the takes, by the bid they sold at")
for lo, hi in ((0.0, 0.85), (0.85, 0.90), (0.90, 0.95), (0.95, 1.01)):
    line(f"sold at {lo:.2f} to {hi:.2f}", [r for r in out if r[2] and lo <= r[3] < hi])
print("\n5. the takes, by the time left at the sale")
for lo, hi in ((600, 901), (300, 600), (180, 300), (0, 180)):
    line(f"{lo} to {hi} s left", [r for r in out if r[2] and lo <= r[4] < hi])
print("DONE")
