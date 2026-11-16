"""The max-loss stop's wait, scaled heavily with the time left (2026-11-16, user, on DOGE 09:30 YES sold at 0.36 after a 34 s wait with 507 s left, which then won: "we still have a problem with selling early ... before 5 mins remaining it doesn't usually matter"; "5 mins isn't always 5 mins"; "it needs to scale, we already know that. and heavily with time").

One question on the same bets (CLAUDE.md rule 4): every live crypto_multiscale bet since 2026-11-14 17:02 ET that the max-loss stop sold and that has quotes on the tape, against what happened. One stated wait, nothing fitted:
  squared   wait = 0.5 x time left x (time left / 900) x (1 - depth): 288 s with 12 minutes left, 141 s with 8.4, 50 s with 5, 18 s with 3, 2 s with 1, before the depth factor (depth = how far the chance is under 0.50, as the live wait has it). The whole stop condition has to hold for the whole wait; it starts over when the condition stops holding.
  linear    wait = 0.5 x time left, the wait the lane had until 2026-11-14 (three bets rode to nothing inside it), for reference.
The stop condition as live: the loss is at least 0.5 x the chance (the bid net of its fee), bid at least 0.05, no sale in the last 8 s. NOT replayed: the long layer's hold and the squeeze (so the replay's stop can begin a little before or after the live one). A sale fills at the bid one second after the decision. Fee on every leg.
Shapes: the time left when the live stop sold.

Run on the host: scripts/cloud.sh run scripts/stop_wait_time_replay.py
"""
import bisect
import datetime

from kalshi_moonshot import config, db
from kalshi_moonshot.strategy import fee_per_contract as fee

ET = datetime.timezone(datetime.timedelta(hours=-5))
SINCE = datetime.datetime(2026, 11, 14, 17, 2, tzinfo=ET).timestamp()
s = config.load()
st = db.connect(s.db_path, "main", shared=True)
tape = db.connect(s.tape_path, "tape", shared=True)
res = {w["ticker"]: (w["result"], float(w["close_ts"])) for w in st.execute("SELECT ticker, close_ts, result FROM windows WHERE close_ts>? AND result IN ('yes','no')", (SINCE,)).fetchall()}


def wait(kind, q, left):
    if kind == "linear":
        return 0.5 * left
    depth = min(1.0, max(0.0, (0.5 - q) / 0.5))
    return 0.5 * left * (left / 900.0) * (1.0 - depth)


out = []  # (left at the live sale, what happened, squared, linear, won)
no_tape = 0
for r in st.execute("SELECT ts, ticker, side, ask, stake_usd, exit_price, exit_ts, exit_reason FROM decisions WHERE lane='crypto_multiscale' AND mode='trade' AND action='bet' AND stake_usd>0 AND ts>? AND exit_reason LIKE '%(max loss)%' ORDER BY ts", (SINCE,)).fetchall():
    if r["ticker"] not in res or r["exit_price"] is None or not r["ask"]:
        continue
    result, close = res[r["ticker"]]
    side, ts0 = r["side"], float(r["ts"])
    q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? ORDER BY ts", (r["ticker"], ts0 - 5, close)).fetchall()
    q = [(float(x[0]), float(x[1] or 0.0), float(x[2] or 0.0)) for x in q if x[2] and float(x[2]) > 0 and not (float(x[1] or 0.0) <= 0 and float(x[2]) >= 1)]
    if len(q) < 30 or q[-1][0] < float(r["exit_ts"]):
        no_tape += 1
        continue
    qt = [x[0] for x in q]

    def bid_at(t):
        i = bisect.bisect_right(qt, t) - 1
        return None if i < 0 else (q[i][1] if side == "yes" else 1.0 - q[i][2])

    basis = float(r["ask"]) + fee(float(r["ask"]))
    n = float(r["stake_usd"]) / basis
    won = result == side
    px = float(r["exit_price"])
    got = {"was": (px - fee(px) - basis) * n}
    for kind in ("squared", "linear"):
        since, sold = None, None
        for t in range(int(ts0) + 1, int(close) - 8):
            b = bid_at(t)
            if b is None:
                continue
            net = b - fee(b)
            if not (b >= 0.05 and (net - basis) / basis <= -0.5 * net):
                since = None
                continue
            since = t if since is None else since
            if t - since >= wait(kind, net, close - t):
                nb = bid_at(t + 1)
                sold = nb - fee(nb)
                break
        got[kind] = ((sold if sold is not None else (1.0 if won else 0.0)) - basis) * n
    out.append((close - float(r["exit_ts"]), got["was"], got["squared"], got["linear"], won))


def line(name, rs):
    if not rs:
        print(f"  {name:22s} n   0")
        return
    was, sq, li = sum(x[1] for x in rs), sum(x[2] for x in rs), sum(x[3] for x in rs)
    print(f"  {name:22s} n {len(rs):3d} ({sum(1 for x in rs if x[4])} won if held) | what happened ${was:+8.2f} | squared ${sq:+8.2f} ({sq - was:+7.2f}; beat {sum(1 for x in rs if x[2] > x[1] + 0.25)}, lost to {sum(1 for x in rs if x[2] < x[1] - 0.25)}) | linear ${li:+8.2f} ({li - was:+7.2f}; beat {sum(1 for x in rs if x[3] > x[1] + 0.25)}, lost to {sum(1 for x in rs if x[3] < x[1] - 0.25)})")


print(f"window: since 2026-11-14 17:02 ET; {len(out)} max-loss sales replayed, {no_tape} more without quotes on the tape")
line("all", out)
print("\nby the time left when the live stop sold")
for lo, hi, name in ((480, 901, "over 8 min"), (300, 480, "5 to 8 min"), (180, 300, "3 to 5 min"), (0, 180, "under 3 min")):
    line(name, [x for x in out if lo <= x[0] < hi])
print("DONE")
