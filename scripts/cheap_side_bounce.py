"""The cheap side bought early and sold on the bounce (2026-11-16, user, on how they buy by hand: "i never buy in the first 2-3 mins unless i see a 85+ market, i'll bet the 15 or lower side because it almost always will return, even if the return is only for 30c or 50c. you still doubled your money at that").

One question, scored as a round trip (Kalshi's quotes only): in every settled market of the live crypto lane's series since Monday 2026-11-09, the FIRST moment a side's ask is 0.15 or under (and at least 0.03; spread 0.03 or under). Bought at the ask. Then, from the quotes that follow: did that side's BID reach twice the price paid, 0.30, 0.50? And what each plan returned, fee on every leg: a resting sale at twice the price paid (else held to settlement), a resting sale at 0.30, at 0.50, and holding to settlement.
By when in the market the buy came (the user's rule is the first three minutes), and by how fast the side had fallen to that price (its drop over the last 60 s, in cents), since a side knocked down in seconds is not the same curve as one that drifted there. No coin is named. The plans and bands were fixed before the run.

Run on the host: scripts/cloud.sh run scripts/cheap_side_bounce.py
"""
import bisect
import datetime

from kalshi_moonshot import config, db
from kalshi_moonshot.lanes import lane_specs
from kalshi_moonshot.strategy import fee_per_contract as fee

ET = datetime.timezone(datetime.timedelta(hours=-5))
SINCE = datetime.datetime(2026, 11, 9, 0, 0, tzinfo=ET).timestamp()
s = config.load()
st = db.connect(s.db_path, "main", shared=True)
tape = db.connect(s.tape_path, "tape", shared=True)
series = set(lane_specs(s)["crypto_multiscale"].series)
wins = [w for w in st.execute("SELECT ticker, series, close_ts, result FROM windows WHERE close_ts>? AND result IN ('yes','no') ORDER BY close_ts", (SINCE,)).fetchall() if w["series"] in series]
ev = []  # (seconds in, ask, max later bid, won, drop over the last 60 s)
seen = 0
for w in wins:
    close = float(w["close_ts"])
    opened = close - 900.0
    q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? ORDER BY ts", (w["ticker"], opened, close - 5)).fetchall()
    q = [(float(x[0]), float(x[1] or 0.0), float(x[2] or 0.0)) for x in q if x[2] and float(x[2]) > 0 and not (float(x[1] or 0.0) <= 0 and float(x[2]) >= 1)]
    if len(q) < 50:
        continue
    seen += 1
    qt = [x[0] for x in q]
    hit = None
    for i, (t, bid, ask) in enumerate(q):
        if ask - bid > 0.03:
            continue
        for side in ("yes", "no"):
            a = ask if side == "yes" else 1 - bid
            if 0.03 <= a <= 0.15:
                hit = (i, side, a)
                break
        if hit:
            break
    if not hit:
        continue
    i, side, a = hit
    later = max(((x[1] if side == "yes" else 1 - x[2]) for x in q[i + 1:]), default=0.0)
    j = bisect.bisect_right(qt, q[i][0] - 60.0) - 1
    before = None if j < 0 else ((q[j][1] + q[j][2]) / 2 if side == "yes" else 1 - (q[j][1] + q[j][2]) / 2)
    now_mid = (q[i][1] + q[i][2]) / 2 if side == "yes" else 1 - (q[i][1] + q[i][2]) / 2
    ev.append((q[i][0] - opened, a, later, w["result"] == side, None if before is None else before - now_mid))


def plan(rs, target):
    """Net per dollar: sold at `target` (a function of the ask) when the later bid reached it, else settled."""
    tot = 0.0
    for _, a, later, won, _ in rs:
        cost = a + fee(a)
        tg = target(a) if target else None
        got = (tg - fee(tg)) if tg is not None and later >= tg else (1.0 if won else 0.0)
        tot += got / cost - 1.0
    return tot / len(rs) * 100


def line(name, rs):
    if len(rs) < 20:
        print(f"  {name:26s} n {len(rs):4d}")
        return
    n = len(rs)
    print(f"  {name:26s} n {n:4d} | ask {sum(r[1] for r in rs) / n:.3f} | reached 2x {sum(1 for r in rs if r[2] >= 2 * r[1]) / n:4.0%}, 0.30 {sum(1 for r in rs if r[2] >= 0.30) / n:4.0%}, 0.50 {sum(1 for r in rs if r[2] >= 0.50) / n:4.0%}, won {sum(1 for r in rs if r[3]) / n:4.0%} | net per $: sell at 2x {plan(rs, lambda a: 2 * a):+6.1f}%, at 0.30 {plan(rs, lambda a: 0.30):+6.1f}%, at 0.50 {plan(rs, lambda a: 0.50):+6.1f}%, hold {plan(rs, None):+6.1f}%")


print(f"window: 2026-11-09 00:00 ET on; {len(wins)} settled markets, {seen} with quotes, {len(ev)} where a side first reached an ask of 0.15 or under")
line("all", ev)
print("\nby when in the market the side first got that cheap")
for lo, hi in ((0, 3), (3, 6), (6, 9), (9, 12), (12, 15)):
    line(f"{lo} to {hi} min in", [r for r in ev if lo * 60 <= r[0] < hi * 60])
print("\nfirst three minutes, by how fast it fell there (drop of the side's mid over the last 60 s)")
early = [r for r in ev if r[0] < 180 and r[4] is not None]
for lo, hi, name in ((-1, 0.05, "under 5c"), (0.05, 0.15, "5 to 15c"), (0.15, 1, "15c and over")):
    line(name, [r for r in early if lo <= r[4] < hi])
print("DONE")
