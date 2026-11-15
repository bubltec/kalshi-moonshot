"""Do buys made right after a fresh, sharp move lose more? And did the buys change after any of today's releases? (2026-11-14 night, user: "i feel like something we just released caused all these loses"; six stops in the 22:00 hour, bought after a 2 to 3 typical-minute drop that fully reversed.)
One question, same bets: every live crypto_multiscale buy since Monday 2026-11-09 00:00 ET (the logic was rewritten then), its real result (its own exit, or settlement; fee on both legs) split by curve features read from the settlement index at the buy: (1) fresh: the spot's move over the last 3 minutes toward the side's win (+) or against it (-), in the market's own typical minutes (the rms of the index's one-minute moves over the last hour); (2) cushion: the side's distance from the strike over the time left, in that rate (z, negative = on the wrong side); (3) hour: the spot's move over the last hour toward the side (+) or against (-), in hourly sigmas; (4) time left. Terciles of each feature over all the buys, the cutoffs read from the sample itself, not fixed. Then the same buys split by the time of the buy around today's releases (21:03 cushion check, 21:30 slope, 22:00 cleanup 2b-A, 22:30 flat hold and look back) to see whether the character of the buys changed.
Run on the host: python scripts/buys_fresh_move_study.py"""
import bisect
import datetime
import json
import math
import urllib.request

from kalshi_moonshot import config, db, variants
from kalshi_moonshot.strategy import fee_per_contract

ET = datetime.timezone(datetime.timedelta(hours=-5))
SINCE = datetime.datetime(2026, 11, 9, 5, 0, tzinfo=datetime.UTC).timestamp()
s = config.load()
st = db.connect(s.db_path, "main", shared=True)
tape = db.connect(s.tape_path, "tape", shared=True)
rows = st.execute("SELECT ts, ticker, side, contracts, stake_usd, exit_price, outcome, close_time FROM decisions WHERE lane='crypto_multiscale' AND mode='trade' AND action='bet' AND stake_usd>0 AND ts>? AND ticker LIKE '%15M-%' ORDER BY ts", (SINCE,)).fetchall()
strikes = {}


def strike_of(ticker):
    if ticker not in strikes:
        try:
            with urllib.request.urlopen(f"https://api.elections.kalshi.com/trade-api/v2/markets/{ticker}", timeout=10) as f:
                m = json.load(f)["market"]
            strikes[ticker] = (float(m["floor_strike"]), m.get("result"))
        except Exception:
            strikes[ticker] = (None, None)
    return strikes[ticker]


feat = []
for r in rows:
    t = float(r["ts"]); side = r["side"]; sign = 1.0 if side == "yes" else -1.0
    n, cost = float(r["contracts"]), float(r["stake_usd"])
    if r["exit_price"] is not None:
        v = n * (float(r["exit_price"]) - fee_per_contract(float(r["exit_price"]))) - cost
    elif r["outcome"] in ("yes", "no"):
        v = (n if r["outcome"] == side else 0.0) - cost
    else:
        continue
    strike, _ = strike_of(r["ticker"])
    ix = variants.spot_index(r["ticker"].split("-")[0], "")
    if strike is None or not ix:
        continue
    close = datetime.datetime.fromisoformat(str(r["close_time"]).replace("Z", "+00:00")).timestamp(); left = close - t
    got = tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND ts>? AND ts<=? AND value>0 ORDER BY ts", (ix, t - 3700.0, t)).fetchall()
    if len(got) < 300 or left <= 0:
        continue
    ts = [float(x[0]) for x in got]; vs = [float(x[1]) for x in got]
    at = lambda x: vs[max(bisect.bisect_right(ts, x) - 1, 0)]
    lv = [math.log(at(t - 60.0 * k)) for k in range(61)]
    mv = [lv[k] - lv[k + 1] for k in range(60)]
    rms = math.sqrt(sum(m * m for m in mv) / 60.0)
    if rms <= 0:
        continue
    spot = at(t)
    z = sign * math.log(spot / strike) / (rms / math.sqrt(60.0) * math.sqrt(left))
    fresh = sign * math.log(spot / at(t - 180.0)) / rms
    hour = sign * math.log(spot / at(t - 3600.0)) / (rms * math.sqrt(60.0))
    feat.append(dict(t=t, ticker=r["ticker"], side=side, v=v, cost=cost, z=z, fresh=fresh, hour=hour, left=left))
print(f"live crypto_multiscale buys since Mon 2026-11-09 with a result and features: {len(feat)}; net {sum(f['v'] for f in feat):+.2f} on ${sum(f['cost'] for f in feat):.0f} staked")


def terciles(key):
    xs = sorted(f[key] for f in feat)
    return xs[len(xs) // 3], xs[2 * len(xs) // 3]


for key, label in (("fresh", "fresh move over the last 3 min toward the side (typical minutes; negative = against)"), ("z", "cushion z at the buy (negative = wrong side of the strike)"), ("hour", "hour trend toward the side (hourly sigmas; negative = against)"), ("left", "seconds left at the buy")):
    lo, hi = terciles(key)
    print(f"\n{label}: terciles cut at {lo:+.2f} and {hi:+.2f}")
    for name, sel in (("lowest third", lambda f: f[key] <= lo), ("middle third", lambda f: lo < f[key] <= hi), ("highest third", lambda f: f[key] > hi)):
        g = [f for f in feat if sel(f)]
        if g:
            print(f"   {name:13s} n={len(g):3d} net {sum(f['v'] for f in g):+8.2f}  per $1 staked {sum(f['v'] for f in g) / sum(f['cost'] for f in g):+.3f}  lost {sum(1 for f in g if f['v'] < 0)} of {len(g)}")
print("\nby the time of the buy around today's releases (ET):")
cuts = [("before today", 0, datetime.datetime(2026, 11, 14, 0, 0, tzinfo=ET).timestamp()), ("today to 21:03", datetime.datetime(2026, 11, 14, 0, 0, tzinfo=ET).timestamp(), datetime.datetime(2026, 11, 14, 21, 3, tzinfo=ET).timestamp()), ("21:03 to 21:30 (cushion check)", datetime.datetime(2026, 11, 14, 21, 3, tzinfo=ET).timestamp(), datetime.datetime(2026, 11, 14, 21, 31, tzinfo=ET).timestamp()), ("21:31 to 22:00 (slope)", datetime.datetime(2026, 11, 14, 21, 31, tzinfo=ET).timestamp(), datetime.datetime(2026, 11, 14, 22, 0, tzinfo=ET).timestamp()), ("22:00 to 22:30 (cleanup 2b-A)", datetime.datetime(2026, 11, 14, 22, 0, tzinfo=ET).timestamp(), datetime.datetime(2026, 11, 14, 22, 31, tzinfo=ET).timestamp()), ("22:31 on (flat hold, 12%)", datetime.datetime(2026, 11, 14, 22, 31, tzinfo=ET).timestamp(), 9e12)]
for name, a, b in cuts:
    g = [f for f in feat if a <= f["t"] < b]
    if g:
        med = lambda k: sorted(f[k] for f in g)[len(g) // 2]
        print(f"   {name:34s} n={len(g):3d} net {sum(f['v'] for f in g):+8.2f} per $1 {sum(f['v'] for f in g) / sum(f['cost'] for f in g):+.3f} | median z {med('z'):+.2f} fresh {med('fresh'):+.2f} hour {med('hour'):+.2f} left {med('left'):.0f}s")
print("\nthe six stops of the 22:00 hour and HYPE, with each feature's rank among all buys (0% = lowest, 100% = highest):")
rank = lambda key, val: 100.0 * sum(1 for f in feat if f[key] <= val) / len(feat)
for f in feat:
    if datetime.datetime(2026, 11, 14, 22, 0, tzinfo=ET).timestamp() <= f["t"] and f["v"] < -8:
        print(f"   {datetime.datetime.fromtimestamp(f['t'], ET):%H:%M:%S} {f['ticker'][2:-3]:22s} {f['side']:3s} {f['v']:+7.2f} | fresh {f['fresh']:+.2f} (rank {rank('fresh', f['fresh']):.0f}%) z {f['z']:+.2f} (rank {rank('z', f['z']):.0f}%) hour {f['hour']:+.2f} (rank {rank('hour', f['hour']):.0f}%) left {f['left']:.0f}s")
print("DONE")
