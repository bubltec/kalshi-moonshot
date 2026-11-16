"""No max-loss sale early in a market (2026-11-16, user: "we still have a problem with selling early"; "5 mins isn't always 5 mins. sometimes its 7, sometimes its 3, sometimes its 1"; "Go live with it").

One question on the same bets (CLAUDE.md rule 4): every live crypto_multiscale max-loss sale since 2026-11-14 17:02 ET with quotes on the tape, against what happened. A sale the rule blocks is held to settlement (the stop stays off for that bet). Two ways to say "early", stated before the run:
  reach   the market can still travel back to even: 0.50 less the sale's net bid is no more than the market's own typical minute x sqrt(minutes left) (the typical minute: root-mean-square 1 s change of the mid from the open to the sale, x sqrt(60)). In the market's own units: a wild market is "early" for longer than a quiet one.
  clock   more than 8 minutes are left (the line the table of 2026-11-16-loss-sale-vs-hold.txt showed; chosen after seeing that table, and said to be so).
Fee on every leg. Shapes: the time left at the sale.

Run on the host: scripts/cloud.sh run scripts/stop_early_latch_replay.py
"""
import bisect
import datetime
import math

from kalshi_moonshot import config, db
from kalshi_moonshot.strategy import fee_per_contract as fee

ET = datetime.timezone(datetime.timedelta(hours=-5))
SINCE = datetime.datetime(2026, 11, 14, 17, 2, tzinfo=ET).timestamp()
s = config.load()
st = db.connect(s.db_path, "main", shared=True)
tape = db.connect(s.tape_path, "tape", shared=True)
res = {w["ticker"]: (w["result"], float(w["close_ts"])) for w in st.execute("SELECT ticker, close_ts, result FROM windows WHERE close_ts>? AND result IN ('yes','no')", (SINCE,)).fetchall()}
out = []  # (left, what happened, held, reach blocks, clock blocks, ratio)
no_tape = 0
for r in st.execute("SELECT ts, ticker, side, ask, stake_usd, exit_price, exit_ts FROM decisions WHERE lane='crypto_multiscale' AND mode='trade' AND action='bet' AND stake_usd>0 AND ts>? AND exit_reason LIKE '%(max loss)%' ORDER BY ts", (SINCE,)).fetchall():
    if r["ticker"] not in res or r["exit_price"] is None or not r["ask"]:
        continue
    result, close = res[r["ticker"]]
    at = float(r["exit_ts"])
    q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? ORDER BY ts", (r["ticker"], close - 920.0, at)).fetchall()
    q = [(float(x[0]), (float(x[1] or 0.0) + float(x[2])) / 2) for x in q if x[2] and float(x[2]) > 0 and not (float(x[1] or 0.0) <= 0 and float(x[2]) >= 1)]
    if not q or at - q[0][0] < 120.0 or q[-1][0] < at - 30:
        no_tape += 1
        continue
    ts = [x[0] for x in q]
    start = max(close - 900.0, ts[0])
    seq = [q[max(bisect.bisect_right(ts, start + k) - 1, 0)][1] for k in range(int(at - start) + 1)]
    sq = sum((b - a) ** 2 for a, b in zip(seq, seq[1:]))
    left = close - at
    unit = math.sqrt(sq / (len(seq) - 1)) * math.sqrt(60.0) if sq > 0 else 0.0
    px = float(r["exit_price"])
    net = px - fee(px)
    basis = float(r["ask"]) + fee(float(r["ask"]))
    n = float(r["stake_usd"]) / basis
    was, held = (net - basis) * n, ((1.0 if result == r["side"] else 0.0) - basis) * n
    reach = unit * math.sqrt(left / 60.0)
    out.append((left, was, held, 0.5 - net <= reach, left > 480.0, (0.5 - net) / reach if reach > 0 else 99.0))


def line(name, rs):
    if not rs:
        print(f"  {name:16s} n   0")
        return
    was = sum(x[1] for x in rs)
    parts = []
    for i, label in ((3, "reach"), (4, "clock")):
        b = [x for x in rs if x[i]]
        new = sum(x[2] if x[i] else x[1] for x in rs)
        parts.append(f"{label}: blocks {len(b):2d} ({sum(1 for x in b if x[2] > 0)} won), ${new:+8.2f} ({new - was:+7.2f})")
    print(f"  {name:16s} n {len(rs):3d} | what happened ${was:+8.2f} | " + " | ".join(parts))


print(f"window: since 2026-11-14 17:02 ET; {len(out)} max-loss sales with quotes, {no_tape} more without")
line("all", out)
print("\nby the time left when the live stop sold")
for lo, hi, name in ((480, 901, "over 8 min"), (300, 480, "5 to 8 min"), (180, 300, "3 to 5 min"), (0, 180, "under 3 min")):
    line(name, [x for x in out if lo <= x[0] < hi])
print("\nthe reach rule by how far under even the sale was, in reaches (under 1 = blocked)")
for lo, hi in ((0, 0.5), (0.5, 1.0), (1.0, 1.5), (1.5, 2.5), (2.5, 100)):
    rs = [x for x in out if lo <= x[5] < hi]
    print(f"  {lo:.1f} to {hi:.1f}: n {len(rs):2d} | won if held {sum(1 for x in rs if x[2] > 0):2d} | hold less sale ${sum(x[2] - x[1] for x in rs):+8.2f}")
print("DONE")
