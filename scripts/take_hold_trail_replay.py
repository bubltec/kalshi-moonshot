"""Would a profit take that counts how long the bet has been held, or a trail on the gain, have done better on the live crypto_multiscale bets? (2026-11-16, user, on KXXRP15M-26NOV161745 NO bought at 0.60 at
minute 7, up to 0.80 with 5 minutes left, sold at 0.25: "why would we not sell when its like +20c ... with over 5 mins left?"; "the sell for profit workflow needs to consider how long we have held the buy for
also"; "sure replay it first".)

One question, same bets (CLAUDE.md rule 4): every live buy of crypto_multiscale since 2026-11-14 17:02 ET (when its profit take went live), against what actually happened to it (the lane's own sale, or
settlement), fees on every leg. Each bet is followed second by second from its buy to the moment it actually ended; a rule that sells first replaces the ending. Three rules, each stated before the run, none fitted:
  A  hold clock: the live take with one change. Its bar is the share of the bet's OWN time used (seconds held over the seconds that were left at the buy) times the bet's room, where the live bar uses the
     market's clock (seconds since the open over 900). Slope check, sure hold, early bid and the late cap are as live (slope share 0.08, sure 0.95 / 0.90 late, early 0.95 in the first 360 s).
  B  trail: the bet is ahead by more than the fee to buy back, and its bid is one typical minute of this market (quoteshock.typical_minute) or more under the highest bid since the buy. The sure hold applies.
  C  both: whichever of A and B sells first.
What is NOT replayed: the cushion check (it can only hold a take back, so the replay sells at least as often as a live rule would), the flat-bid hold in the last fifth, and the buy-back after a take (the lane
buys a sold side back after 15 s; every later live bet is in the table as its own row, but a bet the lane would have made after an earlier replayed sale is not). A sale decided at second t fills at the worse of
the bids at t+1 and t+2. The take's settings changed inside the window (sure hold and early bid from 11-15 night); the replay uses today's.
Shapes: where the side's Kalshi mid sat in its own range since the open at the buy (variants.quote_wave_features), and how far into the market the buy was (the reading rule A turns on). No coin is named.
    scripts/cloud.sh run scripts/take_hold_trail_replay.py
"""
import bisect
import datetime
import math
import os

from kalshi_moonshot import config, curverisk, db, quoteshock, variants
from kalshi_moonshot.strategy import fee_per_contract as fee

ET = datetime.timezone(datetime.timedelta(hours=-5))
# 2026-11-17 (user, on the live commodity bets that were ahead and then lost: "Would it?"): the same replay on other lanes and windows. LANE is a SQL LIKE pattern; LIST=1 prints each sale of rule B.
#     scripts/cloud.sh run scripts/take_hold_trail_replay.py LANE=commodity% SINCE=2026-11-16T21:02 LIST=1
_when = lambda k, d: datetime.datetime.strptime(os.environ[k], "%Y-%m-%dT%H:%M").replace(tzinfo=ET).timestamp() if os.environ.get(k) else d
SINCE = _when("SINCE", datetime.datetime(2026, 11, 14, 17, 2, tzinfo=ET).timestamp())
UNTIL = _when("UNTIL", 9e18)
LANE = os.environ.get("LANE", "crypto_multiscale")
SLOPE, SURE, SURE_LATE, EARLY_BID, EARLY_S = 0.08, 0.95, 0.90, 0.95, 360.0  # the lane's live settings
TRAIL_UNITS = 1.0  # rule B: one typical minute under the peak. Stated before the run
s = config.load()
st = db.connect(s.db_path, "main", shared=True)
tape = db.connect(s.tape_path, "tape", shared=True)

rows = st.execute("SELECT id, ts, ticker, side, ask, stake_usd, contracts, exit_price, exit_ts, exit_reason FROM decisions WHERE lane LIKE ? AND mode='trade' AND action='bet' AND stake_usd>0 AND ts>? AND ts<? ORDER BY ts", (LANE, SINCE, UNTIL)).fetchall()
results = {w["ticker"]: (w["result"], float(w["close_ts"])) for w in st.execute("SELECT ticker, close_ts, result FROM windows WHERE close_ts>? AND result IN ('yes','no')", (SINCE,)).fetchall()}
RULES = ("A", "B", "C")
out = []  # one dict a bet
skipped = unsettled = 0
for r in rows:
    ticker, side = r["ticker"], r["side"]
    if ticker not in results:
        unsettled += 1
        continue
    res, close = results[ticker]
    opened, ts0 = close - 900.0, float(r["ts"])
    q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? ORDER BY ts", (ticker, opened - 20, close)).fetchall()
    q = [(float(x[0]), float(x[1] or 0.0), float(x[2] or 0.0)) for x in q if x[2] and float(x[2]) > 0 and not (float(x[1] or 0.0) <= 0 and float(x[2]) >= 1)]
    qt = [x[0] for x in q]
    mids = [(x[0], (x[1] + x[2]) / 2) for x in q]

    def bid_at(t):
        i = bisect.bisect_right(qt, t) - 1
        if i < 0 or t - qt[i] > 60.0:
            return None
        return q[i][1] if side == "yes" else 1.0 - q[i][2]

    stake, n0 = float(r["stake_usd"]), float(r["contracts"] or 0.0)
    ask = float(r["ask"] or 0.0)
    basis = stake / n0 if n0 > 0 else 0.0  # what was paid a contract, fee in (a resting buy pays none)
    if not (0.05 < ask < 0.99) or not q or ts0 >= close - 2:
        skipped += 1
        continue
    if not (ask - 0.06 <= basis <= ask + fee(ask) + 0.06):
        basis = ask + fee(ask)  # decisions.contracts can undercount the fills: fall back to the ask the decision was made at
    n = stake / basis
    won = res == side
    sold = r["exit_price"] is not None and r["exit_ts"] is not None and float(r["exit_ts"]) < close - 1
    actual = (float(r["exit_price"]) - fee(float(r["exit_price"])) if sold else (1.0 if won else 0.0)) - basis
    stop = float(r["exit_ts"]) if sold else close
    own = close - ts0  # the bet's own time: what was left at the buy
    hist, latched, level, unit, peak = [], False, 0.0, None, 0.0
    sale = {}
    for t in range(int(ts0) + 1, int(stop)):
        bid = bid_at(t)
        if bid is None:
            continue
        left = close - t
        if left <= 0:
            break
        if (t - int(ts0)) % 5 == 1:
            unit = quoteshock.typical_minute(mids[:bisect.bisect_right(qt, t)], float(t))
        look = SLOPE * left
        hist.append((float(t), bid))
        while hist and hist[0][0] < t - max(60.0, 2.0 * look):
            hist.pop(0)
        peak = max(peak, bid)
        net = bid - fee(bid)
        gain, room = net - basis, 1.0 - basis
        early = gain > 0 and bid >= EARLY_BID and 900.0 - left <= EARLY_S
        if not early and curverisk.sure_win(bid, left, SURE, SURE_LATE):
            latched, level = True, (SURE if bid >= SURE else SURE_LATE)
        elif latched and unit is not None and unit > 0 and bid < level - unit * math.sqrt(left / 60.0):
            latched = False
        a = gain > 0 and bid >= 0.05 and curverisk.take_profit(gain, room, left, own, fee(bid), unit) and not curverisk.still_rising(hist, float(t), look, unit, False)
        a = early or (a and not latched)
        b = gain > fee(bid) and bid >= 0.05 and unit is not None and unit > 0 and bid <= peak - TRAIL_UNITS * unit and not latched
        for name, hit in (("A", a), ("B", b), ("C", a or b)):
            if hit and name not in sale:
                fills = [x for x in (bid_at(t + 1), bid_at(t + 2)) if x is not None]
                fill = min(fills) if fills else bid
                sale[name] = (fill - fee(fill) - basis, fill, left, t - ts0)
        if len(sale) == len(RULES):
            break
    sh = variants.quote_wave_features(mids, side, opened, ts0)
    how = (r["exit_reason"] or "")
    out.append({
        "id": r["id"], "ticker": ticker, "ts": ts0, "n": n, "won": won, "actual": actual * n, "sale": sale,
        "shape": "no reading" if sh is None else "top fifth of its range" if sh["pos"] >= 0.8 else "lower in its range",
        "into": "bought in the first third" if ts0 - opened < 300 else "bought in the middle third" if ts0 - opened < 600 else "bought in the last third",
        "ended": "the lane's profit take" if sold and "profit take" in how else "the lane's loss sale" if sold and "max loss" in how else "another sale" if sold else "held and won" if won else "held to zero",
    })


def line(name, rs, rule):
    if not rs:
        print(f"  {name:28s} n   0")
        return
    a = sum(r["actual"] for r in rs)
    b = sum((r["sale"][rule][0] * r["n"]) if rule in r["sale"] else r["actual"] for r in rs)
    tk = [r for r in rs if rule in r["sale"]]
    beat = sum(1 for r in tk if r["sale"][rule][0] * r["n"] > r["actual"] + 1e-9)
    lost = sum(1 for r in tk if r["sale"][rule][0] * r["n"] < r["actual"] - 1e-9)
    print(f"  {name:28s} n {len(rs):3d} | what happened ${a:+8.2f} | with the rule ${b:+8.2f} | change ${b - a:+8.2f} | rule sold first {len(tk):3d}: beat {beat}, lost to {lost}")


NAMES = {"A": "A  hold clock (the bar counts the bet's own time)", "B": "B  trail (ahead, and one typical minute under the peak)", "C": "C  both, whichever sells first"}
print(f"window: {LANE} live buys {datetime.datetime.fromtimestamp(SINCE, ET):%m-%d %H:%M} to {datetime.datetime.fromtimestamp(min(UNTIL, datetime.datetime.now().timestamp()), ET):%m-%d %H:%M} ET; {len(out)} settled with quotes (of {len(rows)} placed; {unsettled} not settled, {skipped} without a usable price or quotes)")
print(f"what happened on them: ${sum(r['actual'] for r in out):+.2f}; {sum(r["n"] for r in out):.0f} contracts bought")
for rule in RULES:
    print(f"\n=== {NAMES[rule]}")
    line("all", out, rule)
    print(" by the side's own curve at the buy")
    for k in ("top fifth of its range", "lower in its range", "no reading"):
        line(k, [r for r in out if r["shape"] == k], rule)
    print(" by how far into the market the buy was")
    for k in ("bought in the first third", "bought in the middle third", "bought in the last third"):
        line(k, [r for r in out if r["into"] == k], rule)
    print(" by how the bet actually ended")
    for k in ("the lane's profit take", "the lane's loss sale", "another sale", "held and won", "held to zero"):
        line(k, [r for r in out if r["ended"] == k], rule)
    tk = [r for r in out if rule in r["sale"]]
    if tk:
        held = sorted(r["sale"][rule][3] for r in tk)
        print(f" the rule's sales: median {held[len(held) // 2]:.0f} s after the buy; {sum(1 for h in held if h < 60)} inside the first minute; median bid {sorted(r['sale'][rule][1] for r in tk)[len(tk) // 2]:.2f}")
if os.environ.get("LIST"):
    print("\nrule B's sales, bet by bet")
    for r in out:
        if "B" in r["sale"]:
            v = r["sale"]["B"]
            print(f"  {datetime.datetime.fromtimestamp(r['ts'], ET):%m-%d %H:%M} {r['ticker'][:28]:28s} sold {v[1]:.2f} at {v[3]:.0f} s held, {v[2]:.0f} s left: ${v[0] * r['n']:+.2f} | actual ${r['actual']:+.2f} ({r['ended']})")
print("\nthe bet that raised the question")
for r in out:
    if r["ticker"].startswith("KXXRP15M-26NOV161745"):
        print(f"  {r['ticker']} actual ${r['actual']:+.2f}; " + "; ".join(f"{k}: sold {v[1]:.2f} at {v[3]:.0f} s held, {v[2]:.0f} s left, ${v[0] * r['n']:+.2f}" for k, v in sorted(r["sale"].items())))
print("DONE")
