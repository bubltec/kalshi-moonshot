"""How the profit take's late cap is triggered, on every live take since the take went live (2026-11-14, user: "the issue isn't the setting necessarily, but how it is being used in the code").
curverisk.take_bar caps the bar at half the room once what is left to win (room - gain) is no more than unit x sqrt(left / 60), the market's typical minute over the last 15 minutes scaled to the time left. For each take, at its sale second: the room, the gain, what is left to win, the typical minute as the exit read it (quote mids over the last 900 s), that swing, the ratio of left-to-win to swing (the cap triggers at 1 or under), the typical minute over only the last 120 s, and the bid's own distance from its 15-minute start.
Run on the host: python scripts/take_cap_trigger.py"""
import bisect
import datetime
from statistics import NormalDist

from kalshi_moonshot import config, db, quoteshock
from kalshi_moonshot.strategy import fee_per_contract

N = NormalDist()


def dens(p):
    """How fast a probability moves for a given move of its underlying: the normal density at its own level (max at 0.5, small near 0 or 1)."""
    p = min(max(p, 0.005), 0.995)
    return N.pdf(N.inv_cdf(p))


ET = datetime.timezone(datetime.timedelta(hours=-5))
SINCE = datetime.datetime(2026, 11, 14, 18, 0, tzinfo=datetime.UTC).timestamp()
s = config.load()
st = db.connect(s.db_path, "main", shared=True)
tape = db.connect(s.tape_path, "tape", shared=True)
rows = st.execute("SELECT ticker, side, contracts, stake_usd, exit_price, exit_ts, close_time, exit_reason FROM decisions WHERE lane='crypto_multiscale' AND action='bet' AND stake_usd>0 AND ts>? AND exit_reason LIKE ? ORDER BY exit_ts", (SINCE, "curve risk exit (profit take)%")).fetchall()
print("last columns: the swing re-read at the bid's own price level (the 15-minute typical minute x density at the bid now / mean density over the window), its ratio, and whether the cap would trigger on it")
print("take                    left  room  gain  left-to-win  unit15  swing  ratio  cap?  unit2min  bid 15 min before -> now")
n_cap = 0
n_cap2 = [0]
for r in rows:
    t = float(r["exit_ts"]); close = datetime.datetime.fromisoformat(str(r["close_time"]).replace("Z", "+00:00")).timestamp(); left = close - t
    q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts>? AND ts<=? ORDER BY ts", (r["ticker"], t - 1000.0, t)).fetchall()
    if len(q) < 30:
        continue
    side = r["side"]
    mids = [(float(x[0]), (float(x[1]) + float(x[2])) / 2.0) for x in q]
    ts = [m[0] for m in mids]
    bid = lambda x: (lambda i: (float(q[i][1]) if side == "yes" else 1.0 - float(q[i][2])))(max(bisect.bisect_right(ts, x) - 1, 0))
    entry = float(r["stake_usd"]) / float(r["contracts"]); basis = entry + fee_per_contract(entry)
    x = float(r["exit_price"]); gain = (x - fee_per_contract(x)) - basis; room = 1.0 - basis; win = room - gain
    u15 = quoteshock.typical_minute(mids, t, 900.0); u2 = quoteshock.typical_minute(mids, t, 120.0)
    if u15 is None:
        continue
    swing = u15 * (left / 60.0) ** 0.5; ratio = win / swing if swing > 0 else float("inf")
    cap = ratio <= 1.0; n_cap += cap
    mean_d = sum(dens(m[1]) for m in mids) / len(mids)
    now_d = dens(bid(t))
    swing2 = swing * now_d / mean_d; ratio2 = win / swing2 if swing2 > 0 else float("inf"); n_cap2[0] += ratio2 <= 1.0
    print(f"{r['ticker'][2:-3]:22s} {left:5.0f} {room:5.3f} {gain:5.3f}  {win:6.3f}      {u15:6.3f} {swing:6.3f} {ratio:5.2f}  {'YES' if cap else 'no '}  {('%.3f' % u2) if u2 else '  -  '}   {bid(t - 900):.2f} -> {x:.2f}   | scaled swing {swing2:.3f} ratio {ratio2:.2f} {'YES' if ratio2 <= 1.0 else 'no'}")
print(f"cap triggered on {n_cap} of {len(rows)} takes; with the swing read at the bid's own price level on {n_cap2[0]}")
print("DONE")
