"""(The same readings, bucketed by the size of the move: do buys after an extreme move do worse?) The entry trend check read from the spot when the market's own history is too young (2026-11-14 night, user: "i think we need to, and that's how i thought it was done from the get").
Three readings at each live crypto_multiscale buy since Mon 2026-11-09 00:00 ET, all "has the side gained over the last 180 s, in its own units (the move over 180 s divided by the standard deviation of 180 s changes over the previous 360 s)":
  own: the side's price in that market's own quotes (the live check since 23:30); no reading when the market has under about 3.5 minutes of quotes;
  spot: the settlement index's move toward the side's win (+) or against it (-), the same arithmetic on the index's ticks, readable from the first second of the market;
  hybrid: own when it has a reading, else spot.
Kept (z over 0) and dropped (z of 0 or under, or no reading) buys: n, net dollars and net per $1 staked (real exit or settlement, fee on both legs), for all buys and for the early ones (under 3.5 minutes after the open, where the own reading is not available). Chosen after the earlier studies; said so.
Run on the host: python scripts/entry_spot_trend_study.py"""
import bisect
import datetime
import statistics

from kalshi_moonshot import config, db, variants
from kalshi_moonshot.strategy import fee_per_contract

ET = datetime.timezone(datetime.timedelta(hours=-5))
SINCE = datetime.datetime(2026, 11, 9, 5, 0, tzinfo=datetime.UTC).timestamp()
SPAN, WINDOW = 180.0, 360.0
s = config.load()
st = db.connect(s.db_path, "main", shared=True)
tape = db.connect(s.tape_path, "tape", shared=True)
rows = st.execute("SELECT ts, ticker, side, contracts, stake_usd, exit_price, outcome, close_time FROM decisions WHERE lane='crypto_multiscale' AND mode='trade' AND action='bet' AND stake_usd>0 AND ts>? AND ticker LIKE '%15M-%' ORDER BY ts", (SINCE,)).fetchall()


def z_series(ts, vals, t, sign=1.0):
    """The move over SPAN s in units of the standard deviation of SPAN-second changes over the previous WINDOW s, on a 1 s grid carrying the last value forward. None with under 20 valid steps."""
    at = lambda x: vals[bisect.bisect_right(ts, x) - 1] if bisect.bisect_right(ts, x) - 1 >= 0 else None
    cur, then = at(t), at(t - SPAN)
    if cur is None or then is None:
        return None
    n = int(WINDOW - SPAN)
    grid = [at(t - 5.0 - k) for k in range(int(WINDOW))]
    steps = [a - b for a, b in zip(grid[:n], grid[int(SPAN):int(SPAN) + n]) if a is not None and b is not None]
    if len(steps) < 20:
        return None
    sd = statistics.pstdev(steps)
    mv = sign * (cur - then)
    return mv / sd if sd > 0 else (0.0 if mv == 0 else 10.0 * (1 if mv > 0 else -1))


buys = []
import math
for r in rows:
    t = float(r["ts"]); side = r["side"]; sign = 1.0 if side == "yes" else -1.0
    n, cost = float(r["contracts"]), float(r["stake_usd"])
    if r["exit_price"] is not None:
        v = n * (float(r["exit_price"]) - fee_per_contract(float(r["exit_price"]))) - cost
    elif r["outcome"] in ("yes", "no"):
        v = (n if r["outcome"] == side else 0.0) - cost
    else:
        continue
    close = datetime.datetime.fromisoformat(str(r["close_time"]).replace("Z", "+00:00")).timestamp()
    elapsed = 900.0 - (close - t)
    q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts>? AND ts<=? ORDER BY ts", (r["ticker"], t - 500.0, t)).fetchall()
    own = None
    if len(q) >= 6 and t - float(q[-1][0]) <= 30.0:
        qt = [float(x[0]) for x in q]; qm = [((float(x[1]) + float(x[2])) / 2.0 if side == "yes" else 1.0 - (float(x[1]) + float(x[2])) / 2.0) for x in q]
        own = z_series(qt, qm, t)
    ix = variants.spot_index(r["ticker"].split("-")[0], "")
    spot = None
    if ix:
        g = tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND ts>? AND ts<=? AND value>0 ORDER BY ts", (ix, t - 600.0, t)).fetchall()
        if len(g) > 100:
            gt = [float(x[0]) for x in g]; gv = [math.log(float(x[1])) for x in g]
            spot = z_series(gt, gv, t, sign)
    buys.append(dict(v=v, cost=cost, elapsed=elapsed, own=own, spot=spot, hybrid=own if own is not None else spot))
print(f"live crypto_multiscale buys since Mon 2026-11-09 with a result: {len(buys)}; net {sum(b['v'] for b in buys):+.2f}; early (under 3.5 min after the open): {sum(1 for b in buys if b['elapsed'] < 210)}")


def line(name, grp):
    if not grp:
        return f"   {name:8s} n=  0"
    return f"   {name:8s} n={len(grp):3d} net {sum(b['v'] for b in grp):+8.2f} per $1 {sum(b['v'] for b in grp) / sum(b['cost'] for b in grp):+.3f} lost {sum(1 for b in grp if b['v'] < 0)}"


print("\nby the size of the 180 s move, in the market's own units (z): net and per $1 staked")
for key in ("own", "spot"):
    print(f"  reading: {key}")
    edges = [(-1e9, 0.0, "z <= 0"), (0.0, 1.0, "0 < z <= 1"), (1.0, 2.0, "1 < z <= 2"), (2.0, 3.0, "2 < z <= 3"), (3.0, 1e9, "z > 3")]
    for lo, hi, name in edges:
        g = [b for b in buys if b[key] is not None and lo < b[key] <= hi]
        print(line(name, g).replace("   ", "    ", 1))
print("DONE")
