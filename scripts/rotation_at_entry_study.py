"""Does the wave's phase at the buy (the short slope rolled over against the long slope) predict how a bet ends? (2026-11-15, user: "crypto moves in waves and directions shift in minutes not hours ... each segment should be a factor of the next ... I could use some help on it, I think you get what I'm aiming for")

One question, same bets (CLAUDE.md rule 4): every crypto_multiscale bet since the width change (2026-11-14 17:02 ET), live and the paper variants (the paper ones only widen the sample; each is its own bet), with its result (its own exit or settlement, fee on every leg). At the buy, from the Kalshi perp's own tape, the side-signed move over the last m minutes in units of that scale's own typical move, z_m = side x (L(t) - L(t - m)) / (s1 x sqrt(m)), s1 the rms of the last hour's one-minute log moves, at the scales m = 2, 3, 5, 8, 13, 21 minutes (a Fibonacci ladder: each scale about 1.6 times the one before, the way curvemodel.py reads them). Positive = the side's price has been rising at that scale. Two readings are scored against the result:
  the rotation: R = z_3 - z_13, the short slope minus the longer one in their own units (the differential between adjacent rungs; negative = the short move has turned against the longer one), and the same for z_5 - z_21;
  the pattern of signs of the short (3 minute) and long (13 and 21 minute) slopes: both up (carrying on), short down with long up (rolled over: a pullback inside a rise), short up with long down (a bounce inside a fall), both down.
By group: n, share that made money, net per dollar staked, average win and loss, and by quintile of R. Not split by curve shape beyond this; the scales themselves are the curve's own features. Said plainly: a first look at whether the phase carries information about a bet that the buy rule (price up over three minutes) does not already use.

Run on the host: python scripts/rotation_at_entry_study.py
"""
import bisect
import datetime
import math
import re
from collections import defaultdict

from kalshi_moonshot import config, db
from kalshi_moonshot.strategy import fee_per_contract
from kalshi_moonshot.watcher import perp_index

ET = datetime.timezone(datetime.timedelta(hours=-5))
import os
SINCE = float(os.environ.get("SINCE", datetime.datetime(2026, 11, 14, 17, 2, tzinfo=ET).timestamp()))  # SINCE=epoch seconds to look at a later stretch
SCALES = (2, 3, 5, 8, 13, 21)
s = config.load()
st = db.connect(s.db_path, "main", shared=True)
tape = db.connect(s.tape_path, "tape", shared=True)
ticks = {}


def series_ticks(ser):
    if ser not in ticks:
        kp = perp_index(ser)
        rows = tape.execute("SELECT DISTINCT ON (FLOOR(ts / 60)) ts, value FROM index_ticks WHERE index_id=? AND ts>? AND value>0 ORDER BY FLOOR(ts / 60), ts", (kp, SINCE - 7200)).fetchall() if kp else []
        ticks[ser] = ([float(r[0]) for r in rows], [math.log(float(r[1])) for r in rows])
    return ticks[ser]


bets = []
for r in st.execute("SELECT lane, ts, ticker, side, contracts, stake_usd, exit_price, outcome FROM decisions WHERE lane LIKE 'crypto_multiscale%' AND action='bet' AND stake_usd>0 AND ts>? ORDER BY ts", (SINCE,)).fetchall():
    n, cost = float(r["contracts"] or 0), float(r["stake_usd"])
    m = re.match(r"(KX[A-Z]+15M)-", r["ticker"])
    if n <= 0 or not m:
        continue
    if r["exit_price"] is not None:
        x = float(r["exit_price"]); v = n * (x - fee_per_contract(x)) - cost
    elif r["outcome"] in ("yes", "no"):
        v = (n if r["outcome"] == r["side"] else 0.0) - cost
    else:
        continue
    ts, lv = series_ticks(m.group(1))
    if not ts:
        continue
    t = float(r["ts"])

    def lvl(tt):
        i = bisect.bisect_right(ts, tt) - 1
        return lv[i] if i >= 0 and tt - ts[i] <= 180.0 else None
    marks = [lvl(t - 60.0 * k) for k in range(0, 62)]
    if any(x is None for x in marks):
        continue
    s1 = math.sqrt(sum((marks[k] - marks[k + 1]) ** 2 for k in range(60)) / 60.0)
    if s1 <= 0:
        continue
    sign = 1.0 if r["side"] == "yes" else -1.0
    z = {m_: sign * (marks[0] - marks[m_]) / (s1 * math.sqrt(m_)) for m_ in SCALES}
    bets.append((r["lane"] == "crypto_multiscale", v, cost, z, r["ticker"][2:-3], r["side"], float(r["ts"])))
print(f"bets with a result and a perp tape: {len(bets)} (live {sum(1 for b in bets if b[0])}); net ${sum(b[1] for b in bets):+.2f} on ${sum(b[2] for b in bets):.0f}")


def line(name, g):
    if len(g) < 5:
        return
    c = sum(b[2] for b in g)
    w = [b for b in g if b[1] > 0]; l = [b for b in g if b[1] <= 0]
    print(f"  {name:52s} n {len(g):4d} | won {len(w) / len(g):4.0%} | net {sum(b[1] for b in g) / c * 100:+6.1f}% | win ${sum(b[1] for b in w) / max(len(w), 1):5.2f} loss ${-sum(b[1] for b in l) / max(len(l), 1):5.2f}")


def pattern(b):
    z = b[3]
    short = "short up" if z[3] > 0 else "short down"
    long_ = "long up" if (z[13] + z[21]) > 0 else "long down"
    return f"{short}, {long_}"


for label, sel in (("all bets", bets), ("live only", [b for b in bets if b[0]])):
    print(f"\n{label}: by the signs of the 3-minute slope and the 13+21-minute slope (side-signed; up = the side's price rising)")
    g = defaultdict(list)
    for b in sel:
        g[pattern(b)].append(b)
    for k in ("short up, long up", "short down, long up", "short up, long down", "short down, long down"):
        line(k, g.get(k, []))
for name, f in (("R = z3 - z13", lambda z: z[3] - z[13]), ("R = z5 - z21", lambda z: z[5] - z[21])):
    print(f"\nall bets by quintile of {name} (low = the short slope has turned against the long one)")
    srt = sorted(bets, key=lambda b: f(b[3]))
    for q in range(5):
        g = srt[q * len(srt) // 5:(q + 1) * len(srt) // 5]
        if g:
            line(f"quintile {q + 1} (R {f(g[0][3]):+.2f} to {f(g[-1][3]):+.2f})", g)
print("\nall bets by how far the side had already run over 13 minutes (z13): extended = a long move already made")
for lo, hi in ((-99, -0.5), (-0.5, 0.5), (0.5, 1.5), (1.5, 99)):
    line(f"z13 {lo:+.1f} to {hi:+.1f}", [b for b in bets if lo <= b[3][13] < hi])
print("\nwhat skipping the flagged buys would have changed (same bets, their result removed; thresholds chosen after seeing the table above, so said to be so)")
rules = {"extended: z13 at or over +1.5": lambda z: z[13] >= 1.5, "rolled over: z5 - z21 under -0.3": lambda z: z[5] - z[21] < -0.3, "either": lambda z: z[13] >= 1.5 or z[5] - z[21] < -0.3}
for label, sel in (("all bets", bets), ("live only", [b for b in bets if b[0]])):
    for name, rule in rules.items():
        sk = [b for b in sel if rule(b[3])]
        kept = [b for b in sel if not rule(b[3])]
        print(f"  {label:9s} {name:36s}: skips {len(sk):3d} bets ({sum(1 for b in sk if b[1] > 0)} winners, {sum(1 for b in sk if b[1] <= 0)} losers), change ${-sum(b[1] for b in sk):+8.2f}; the rest: net ${sum(b[1] for b in kept):+8.2f} ({sum(b[1] for b in kept) / max(sum(b[2] for b in kept), 1) * 100:+.1f}% on ${sum(b[2] for b in kept):.0f})")
print("\nlive bets one by one: the gate's two readings and what it would have done (skip = z13 at or over 1.5, or z5 - z21 under -0.3)")
for b in sorted([b for b in bets if b[0]], key=lambda b: b[6]):
    flag = b[3][13] >= 1.5 or b[3][5] - b[3][21] < -0.3
    print(f"  {datetime.datetime.fromtimestamp(b[6], ET):%H:%M} {b[4]:16s} {b[5]:3s} z13 {b[3][13]:+5.2f} z5-z21 {b[3][5] - b[3][21]:+5.2f} -> {'SKIP' if flag else 'let in'} | result ${b[1]:+7.2f}")
live = [b for b in bets if b[0]]
sk = [b for b in live if b[3][13] >= 1.5 or b[3][5] - b[3][21] < -0.3]
ok = [b for b in live if not (b[3][13] >= 1.5 or b[3][5] - b[3][21] < -0.3)]
print(f"\nlive in this stretch: {len(live)} bets, net ${sum(b[1] for b in live):+.2f}")
print(f"  the gate would have SKIPPED {len(sk)}: {sum(1 for b in sk if b[1] > 0)} winners ${sum(b[1] for b in sk if b[1] > 0):+.2f}, {sum(1 for b in sk if b[1] <= 0)} losers ${sum(b[1] for b in sk if b[1] <= 0):+.2f}; net ${sum(b[1] for b in sk):+.2f}")
print(f"  and LET IN {len(ok)}: {sum(1 for b in ok if b[1] > 0)} winners ${sum(b[1] for b in ok if b[1] > 0):+.2f}, {sum(1 for b in ok if b[1] <= 0)} losers ${sum(b[1] for b in ok if b[1] <= 0):+.2f}; net ${sum(b[1] for b in ok):+.2f}")
big = [b for b in ok if b[1] <= -10.0]
print(f"  the let-in losses of $10 or more: {len(big)}: " + ", ".join(f"{b[4]} {b[5]} ${b[1]:+.2f}" for b in big))
print("DONE")
