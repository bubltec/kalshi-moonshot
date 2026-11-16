"""The lane's own loss sales against holding (2026-11-16, user, on DOGE 09:30 YES bought at 0.59, sold by the max-loss stop at 0.36 three minutes later, and the market then settled YES: "long term looks like doge would have won, the spike held").

One question on the same bets (CLAUDE.md rule 4): every live crypto_multiscale bet since 2026-11-14 17:02 ET that the lane sold before the close, by the kind of sale (the exit reason's own label), against holding that bet to settlement. Fee on every leg; contracts from the stake over the all-in ask.
Shapes: how far into the market the sale came, and how long after the buy.

Run on the host: scripts/cloud.sh run scripts/loss_sale_vs_hold.py
"""
import bisect
import datetime
import math
import re
from collections import defaultdict

from kalshi_moonshot import config, db
from kalshi_moonshot.strategy import fee_per_contract as fee

ET = datetime.timezone(datetime.timedelta(hours=-5))
SINCE = datetime.datetime(2026, 11, 14, 17, 2, tzinfo=ET).timestamp()
s = config.load()
st = db.connect(s.db_path, "main", shared=True)
tape = db.connect(s.tape_path, "tape", shared=True)


def reach(ticker, side, close, at):
    """How far this market's own price typically travels in the time left: its typical minute (root-mean-square 1 s change of the mid from the open to the sale, times sqrt(60)) times sqrt(minutes left). None with under two minutes of quotes."""
    q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? ORDER BY ts", (ticker, close - 920.0, at)).fetchall()
    q = [(float(x[0]), (float(x[1] or 0.0) + float(x[2])) / 2) for x in q if x[2] and float(x[2]) > 0 and not (float(x[1] or 0.0) <= 0 and float(x[2]) >= 1)]
    if not q or at - q[0][0] < 120.0:
        return None
    ts = [x[0] for x in q]
    start = max(close - 900.0, ts[0])
    seq = [q[max(bisect.bisect_right(ts, start + k) - 1, 0)][1] for k in range(int(at - start) + 1)]
    sq = sum((b - a) ** 2 for a, b in zip(seq, seq[1:]))
    if sq <= 0:
        return None
    return math.sqrt(sq / (len(seq) - 1)) * math.sqrt(60.0) * math.sqrt(max(close - at, 1.0) / 60.0)
res = {w["ticker"]: (w["result"], float(w["close_ts"])) for w in st.execute("SELECT ticker, close_ts, result FROM windows WHERE close_ts>? AND result IN ('yes','no')", (SINCE,)).fetchall()}
rows = []  # (kind, sale less hold in dollars, won if held, seconds into the market at the sale, seconds held, gain at the sale)
total = 0
for r in st.execute("SELECT ts, ticker, side, ask, stake_usd, exit_price, exit_ts, exit_reason FROM decisions WHERE lane='crypto_multiscale' AND mode='trade' AND action='bet' AND stake_usd>0 AND ts>? ORDER BY ts", (SINCE,)).fetchall():
    if r["ticker"] not in res or not r["ask"] or not (0.05 < float(r["ask"]) < 0.99):
        continue
    total += 1
    result, close = res[r["ticker"]]
    if r["exit_price"] is None or r["exit_ts"] is None or float(r["exit_ts"]) >= close - 1:
        continue
    ask, px = float(r["ask"]), float(r["exit_price"])
    basis = ask + fee(ask)
    n = float(r["stake_usd"]) / basis
    won = result == r["side"]
    sale, hold = px - fee(px) - basis, (1.0 if won else 0.0) - basis
    m = re.search(r"\(([a-z ]+)\)", str(r["exit_reason"] or ""))
    kind = m.group(1) if m else str(r["exit_reason"] or "?")[:30]
    rc = reach(r["ticker"], r["side"], close, float(r["exit_ts"])) if sale < 0 else None
    rows.append((kind, (sale - hold) * n, won, float(r["exit_ts"]) - (close - 900.0), float(r["exit_ts"]) - float(r["ts"]), sale, None if rc is None else -sale / rc))


def line(name, rs):
    if not rs:
        print(f"  {name:34s} n   0")
        return
    w = [x for x in rs if x[2]]
    print(f"  {name:34s} n {len(rs):3d} | would have won held {len(w):3d} ({len(w) / len(rs):4.0%}) | sale less hold ${sum(x[1] for x in rs):+8.2f} | saved ${sum(x[1] for x in rs if x[1] > 0):+8.2f} on {sum(1 for x in rs if x[1] > 0)}, gave up ${sum(x[1] for x in rs if x[1] < 0):+8.2f} on {sum(1 for x in rs if x[1] < 0)}")


print(f"window: since 2026-11-14 17:02 ET; {total} settled live crypto_multiscale bets, {len(rows)} sold before the close")
print("\n1. by the kind of sale")
kinds = defaultdict(list)
for x in rows:
    kinds[x[0]].append(x)
for k, rs in sorted(kinds.items(), key=lambda kv: -len(kv[1])):
    line(k, rs)
loss = [x for x in rows if x[5] < 0]
print("\n2. sales at a loss, by minutes into the market at the sale")
for lo, hi in ((0, 6), (6, 9), (9, 12), (12, 15)):
    line(f"{lo} to {hi} min", [x for x in loss if lo * 60 <= x[3] < hi * 60])
print("\n3. sales at a loss, by how long the bet had been held")
for lo, hi, name in ((0, 120, "under 2 min"), (120, 300, "2 to 5 min"), (300, 900, "5 min and over")):
    line(name, [x for x in loss if lo <= x[4] < hi])
print("\n4. sales at a loss, by the time left at the sale (user, 2026-11-16: \"before 5 mins remaining it doesn't usually matter\")")
line("more than 5 min left", [x for x in loss if x[3] < 600])
line("5 min or less left", [x for x in loss if x[3] >= 600])
for lo, hi in ((0, 420), (420, 600), (600, 720), (720, 810), (810, 900)):
    line(f"  {(900 - hi) // 60}:{(900 - hi) % 60:02d} to {(900 - lo) // 60}:{(900 - lo) % 60:02d} left", [x for x in loss if lo <= x[3] < hi])
print("\n5. sales at a loss, by the loss in units of what the market can still travel (user, 2026-11-16: \"5 mins isn't always 5 mins. sometimes its 7, sometimes its 3, sometimes its 1\"): the loss per contract over this market's own typical minute x sqrt(minutes left). Under 1 = the market typically moves further than that in the time left")
line("no reading (no quotes on the tape)", [x for x in loss if x[6] is None])
for lo, hi, name in ((0, 0.5, "under 0.5 reaches"), (0.5, 1.0, "0.5 to 1"), (1.0, 2.0, "1 to 2"), (2.0, 99, "2 and over")):
    line(name, [x for x in loss if x[6] is not None and lo <= x[6] < hi])
print("DONE")
