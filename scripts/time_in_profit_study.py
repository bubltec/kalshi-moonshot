"""Does the time a bet has spent in profit say when to take it or cut it? (2026-11-17, user: "the profit take is supposed to also consider time held at profit. This is easily achieved with a counter that checks
if the price dipped below the profit point, and how frequently. If that frequency is high as time remaining ends, we should take it. If the market never really became profitable and dragged downward we
should sell it when it's 30c below the buy to avoid a whole loss. Maybe a simpler way ... is by counting the time it has been profitable and it needs to qualify as profitable before 90s remain otherwise
we should sell?")

One question on the bot's own bets (CLAUDE.md rule 4): every live commodity bet since the risk exit went live (2026-11-13 11:02 ET; SINCE/UNTIL=YYYY-MM-DDTHH:MM for another window), followed second by
second on Kalshi's own quote for the side held, fees on every leg, against what actually happened to it (the lane's own sale, or settlement). "In profit" = the bid less the sell fee is over what was
paid, fee in. A sale decided at second t fills at the worse of the bids at t+1 and t+2. Three readings, each the user's idea as stated, with the numbers the user gave (90 s, 30c) and no others fitted:
  1  the simple one: at 90 s left, the bets still held, by the share of their held time they spent in profit. Would selling there have beaten what happened?
  2  the dip counter: at 90 s left, the bets in profit at that moment, by how many times they had fallen back under the profit point. Would taking there have beaten what happened?
  3  the cut: a bet that has spent under a tenth of its held time in profit and whose bid is 30c or more under the price paid is sold there. (The tenth is this script's reading of "never really became
     profitable"; said so.)
The groups in 1 and 2 are the shapes: what the bet's own curve did against its cost. Bets bought with under 120 s left are left out of 1 and 2 (nothing to count). Kalshi's quote only.
    scripts/cloud.sh run scripts/time_in_profit_study.py
"""
import bisect
import datetime
import os
import sys
import traceback

from kalshi_moonshot import config, db
from kalshi_moonshot.strategy import fee_per_contract as fee

sys.excepthook = lambda *e: print("".join(traceback.format_exception(*e)))
ET = datetime.timezone(datetime.timedelta(hours=-5))
when = lambda k, d: datetime.datetime.strptime(os.environ[k], "%Y-%m-%dT%H:%M").replace(tzinfo=ET).timestamp() if os.environ.get(k) else d
SINCE = when("SINCE", datetime.datetime(2026, 11, 13, 11, 2, tzinfo=ET).timestamp())
UNTIL = when("UNTIL", 9e18)
LANE = os.environ.get("LANE", "commodity%")
AT, CUT = 90.0, 0.30  # the user's numbers
s = config.load()
st = db.connect(s.db_path, "main", shared=True)
tape = db.connect(s.tape_path, "tape", shared=True)
results = {w["ticker"]: (w["result"], float(w["close_ts"])) for w in st.execute("SELECT ticker, close_ts, result FROM windows WHERE close_ts>? AND result IN ('yes','no')", (SINCE,)).fetchall()}
rows = st.execute("SELECT ts, ticker, lane, side, ask, stake_usd, contracts, exit_price, exit_ts FROM decisions WHERE lane LIKE ? AND mode='trade' AND action='bet' AND stake_usd>0 AND ts>? AND ts<? ORDER BY ts", (LANE, SINCE, UNTIL)).fetchall()
bets = []
for r in rows:
    if r["ticker"] not in results:
        continue
    res, close = results[r["ticker"]]
    t0, side = float(r["ts"]), r["side"]
    q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (r["ticker"], t0 - 30, close)).fetchall()
    if len(q) < 10:
        continue
    qt = [float(x[0]) for x in q]
    qb = [float(x[1]) if side == "yes" else 1.0 - float(x[2]) for x in q]
    stake, n0, ask = float(r["stake_usd"]), float(r["contracts"] or 0.0), float(r["ask"] or 0.0)
    basis = stake / n0 if n0 > 0 and ask - 0.06 <= stake / n0 <= ask + fee(ask) + 0.06 else ask + fee(ask)
    n = stake / basis
    won = res == side
    sold = r["exit_price"] is not None and r["exit_ts"] is not None and float(r["exit_ts"]) < close - 1
    actual = ((float(r["exit_price"]) - fee(float(r["exit_price"])) if sold else (1.0 if won else 0.0)) - basis) * n
    end = float(r["exit_ts"]) if sold else close
    path = []  # (t, bid) once a second while the bet was really held
    for t in range(int(t0) + 1, int(end) - 1):
        i = bisect.bisect_right(qt, t) - 1
        if i >= 0 and t - qt[i] <= 60:
            path.append((t, qb[i]))
    if not path:
        continue

    def fill(k, path=path):
        got = [b for _, b in path[k + 1 : k + 3]]
        return min(got) if got else path[k][1]

    bets.append({"t0": t0, "close": close, "basis": basis, "ask": ask, "n": n, "won": won, "actual": actual, "path": path, "fill": fill})


def tally(name, pairs):
    """pairs: (bet, sale result or None)."""
    if not pairs:
        print(f"  {name:44s} n   0")
        return
    a = sum(b["actual"] for b, _ in pairs)
    w = sum(v if v is not None else b["actual"] for b, v in pairs)
    tk = [(b, v) for b, v in pairs if v is not None]
    print(f"  {name:44s} n {len(pairs):3d} | won if held {sum(1 for b, _ in pairs if b['won']):3d} | what happened ${a:+8.2f} | sold there ${w:+8.2f} | change ${w - a:+8.2f} | sold {len(tk):3d}: beat {sum(1 for b, v in tk if v > b['actual'] + 1e-9)}, lost to {sum(1 for b, v in tk if v < b['actual'] - 1e-9)}")


def state_at(b, left):
    """At `left` seconds to go: (index in the path, share of held seconds in profit, times it fell back under the profit point, in profit now), or None when the bet was not held then."""
    t_at = b["close"] - left
    if b["t0"] > b["close"] - left - 30 or b["path"][-1][0] < t_at - 2:
        return None
    k = bisect.bisect_right([t for t, _ in b["path"]], t_at) - 1
    if k < 5:
        return None
    flags = [bid - fee(bid) - b["basis"] > 0 for _, bid in b["path"][: k + 1]]
    dips = sum(1 for x, y in zip(flags, flags[1:]) if x and not y)
    return k, sum(flags) / len(flags), dips, flags[-1]


print(f"window: {LANE} live bets {datetime.datetime.fromtimestamp(SINCE, ET):%m-%d %H:%M} to {datetime.datetime.fromtimestamp(min(UNTIL, datetime.datetime.now().timestamp()), ET):%m-%d %H:%M} ET; {len(bets)} settled with quotes; what happened ${sum(b['actual'] for b in bets):+.2f}; {sum(1 for b in bets if b['won'])} won if held")
for left in (AT, 180.0):
    got = [(b, state_at(b, left)) for b in bets]
    got = [(b, v) for b, v in got if v is not None]
    sale = lambda b, v: (b["fill"](v[0]) - fee(b["fill"](v[0])) - b["basis"]) * b["n"]
    print(f"\n=== 1  at {left:.0f} s left, the {len(got)} bets still held (bought at least 30 s earlier), by the share of their held time in profit; 'sold there' sells every bet in the row at that moment")
    for name, lo, hi in (("never in profit", -1, 0.0), ("in profit under a quarter of the time", 0.0, 0.25), ("a quarter to a half", 0.25, 0.5), ("over half", 0.5, 1.01)):
        tally(name, [(b, sale(b, v)) for b, v in got if lo < v[1] <= hi])
    tally("not in profit at that moment", [(b, sale(b, v)) for b, v in got if not v[3]])
    tally("in profit at that moment", [(b, sale(b, v)) for b, v in got if v[3]])
    print(f"=== 2  at {left:.0f} s left, the bets in profit at that moment, by how many times they had fallen back under the profit point")
    for name, lo, hi in (("no dip under the profit point", 0, 0), ("1 dip", 1, 1), ("2 dips", 2, 2), ("3 or more", 3, 999)):
        tally(name, [(b, sale(b, v)) for b, v in got if v[3] and lo <= v[2] <= hi])
    print("     and those NOT in profit at that moment, by the same count")
    for name, lo, hi in (("was never in profit", -1, -1), ("no dip counted (in profit, then fell once)", 1, 1), ("2 or more dips", 2, 999)):
        tally(name, [(b, sale(b, v)) for b, v in got if not v[3] and ((lo == -1 and v[1] == 0) or (lo >= 1 and v[1] > 0 and lo <= v[2] <= hi))])

print(f"\n=== 3  the cut: under a tenth of the held time in profit and the bid {CUT:.2f} or more under the price paid; sold at the next bids")
out = []
for b in bets:
    prof, hit = 0, None
    for k, (t, bid) in enumerate(b["path"]):
        prof += bid - fee(bid) - b["basis"] > 0
        if k >= 5 and prof / (k + 1) < 0.10 and bid <= b["ask"] - CUT and bid >= 0.05:
            f = b["fill"](k)
            hit = ((f - fee(f) - b["basis"]) * b["n"], b["close"] - t, f)
            break
    out.append((b, hit))
tally("all", [(b, h[0] if h else None) for b, h in out])
for name, lo, hi in (("fired with over 300 s left", 300, 9999), ("90 to 300 s left", 90, 300), ("under 90 s left", 0, 90)):
    tally(name, [(b, h[0]) for b, h in out if h and lo < h[1] <= hi])
for name, lo, hi in (("paid under 0.80", 0, 0.80), ("paid 0.80 and over", 0.80, 2)):
    tally(name, [(b, h[0] if h else None) for b, h in out if lo <= b["basis"] < hi])
fs = sorted(h[2] for _, h in out if h)
if fs:
    print(f"  the cut's sales: median bid {fs[len(fs) // 2]:.2f}, median {sorted(h[1] for _, h in out if h)[len(fs) // 2]:.0f} s left")
print("DONE")
