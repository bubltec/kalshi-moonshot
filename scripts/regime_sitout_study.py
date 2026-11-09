"""Should crypto_multiscale sit out when the market is running hot? (2026-11-08, user: "do 3 and 5 from your list"; item 3: sit out hot markets, CLAUDE.md rule 3: a reading outside its market's recent range is its own state and no rule is assumed to hold there).
Question: on the live crypto_multiscale bets since 11-05 (what actually happened: sold by an exit, or held to the result; net after both fees), do the bets taken when the market's volatility was far above its own normal lose more? Two readings at the buy, both in the market's own units (rule 1, no coin names):
  own ratio: this coin's last 15 minutes of one-second root-mean-square log move over the median of its own 15-minute readings of the previous 24 hours.
  hot count: how many of the nine crypto indexes have that ratio at 1.5 or more.
Bands (a grid, fixed before the results): own ratio under 0.8, 0.8 to 1.25, 1.25 to 2, 2 and over; hot count 0-1, 2-4, 5 and over.
Reported per band: bets, net in dollars, net per $ staked with its standard error, win rate (a bet that ended above its cost), two halves each with theirs.
Not covered: that a gate would also change which later bets the lane makes (the gate is not replayed, only the bets it would have skipped are read), the exits' own volatility gates.

    scripts/hostrun3.sh-style: docker compose exec -T bot python - < scripts/regime_sitout_study.py
"""

import math
import os
import statistics

from kalshi_moonshot import config, db, variants
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee

os.nice(15)
s = config.reload()
st = Store(s.db_path, s.exclude_versions)
tape = db.connect(s.tape_path, "tape", shared=True)
SERIES = ("KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M", "KXDOGE15M", "KXNEAR15M", "KXBNB15M", "KXHYPE15M", "KXZEC15M")
rows = st.db.execute(
    "SELECT d.ts,d.ticker,d.side,d.contracts,d.stake_usd,d.exit_price,w.result FROM decisions d LEFT JOIN windows w ON w.ticker=d.ticker "
    "WHERE d.action='bet' AND d.mode='trade' AND d.lane='crypto_multiscale' AND d.contracts>0 AND d.stake_usd>0 AND d.ts>? ORDER BY d.ts",
    (1794000000,),
).fetchall()
bets = []
for r in rows:
    n, cost = float(r["contracts"]), float(r["stake_usd"])
    if r["exit_price"]:
        proceeds = n * (float(r["exit_price"]) - fee(float(r["exit_price"])))
    elif r["result"] in ("yes", "no"):
        proceeds = n if r["result"] == r["side"] else 0.0
    else:
        continue
    bets.append((float(r["ts"]), r["ticker"].split("-")[0], proceeds - cost, cost))
print("live crypto_multiscale bets with an outcome since 11-05:", len(bets))
T0 = int(min(b[0] for b in bets)) - 86400 - 1000
T1 = int(max(b[0] for b in bets)) + 5
PREF = {}
for ser in SERIES:
    ix = variants.crypto_index(ser)
    r = tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts>? AND ts<? AND value>0 ORDER BY ts", (ix, T0 - 5, T1)).fetchall()
    ts = [float(x[0]) for x in r]
    vs = [float(x[1]) for x in r]
    sq, last, j, prev = [0.0], None, 0, None
    for t in range(T0, T1):
        while j < len(ts) and ts[j] <= t:
            last = math.log(vs[j])
            j += 1
        sq.append(sq[-1] + ((last - prev) ** 2 if last is not None and prev is not None else 0.0))
        prev = last
    PREF[ser] = sq


def rms(sq, a, b):
    return math.sqrt(max(sq[b - T0] - sq[a - T0], 0.0) / (b - a))


def ratio(ser, t):
    sq = PREF[ser]
    t = int(t)
    now_rms = rms(sq, t - 900, t)
    base = [rms(sq, t - 900 * (k + 2), t - 900 * (k + 1)) for k in range(96)]
    base = [x for x in base if x > 0]
    return now_rms / statistics.median(base) if base and now_rms > 0 else None


out = []
for ts, ser, net, cost in bets:
    own = ratio(ser, ts)
    hot = sum(1 for o in SERIES if (ratio(o, ts) or 0) >= 1.5)
    if own is not None:
        out.append((ts, own, hot, net, cost))
print("read:", len(out))
mid = sorted(o[0] for o in out)[len(out) // 2]


def ms(xs):
    n = len(xs)
    if n == 0:
        return "n=0"
    m = sum(xs) / n
    se = (sum((x - m) ** 2 for x in xs) / max(n - 1, 1)) ** 0.5 / math.sqrt(n)
    return f"{m:+.3f} +/- {se:.3f} (n={n})"


def line(label, xs):
    if not xs:
        print(f"   {label:22s} n=0")
        return
    per = [o[3] / o[4] for o in xs]
    a = [o[3] / o[4] for o in xs if o[0] <= mid]
    b = [o[3] / o[4] for o in xs if o[0] > mid]
    print(f"   {label:22s} net ${sum(o[3] for o in xs):+8.2f} | per $ staked {ms(per)} | won {100 * sum(1 for o in xs if o[3] > 0) / len(xs):3.0f}% | first half {ms(a)} | second half {ms(b)}")


line("all", out)
print("\nthis coin's volatility over its own 24 h median:")
for lo, hi in ((0, 0.8), (0.8, 1.25), (1.25, 2.0), (2.0, 99)):
    line(f"own ratio {lo:g} to {hi:g}", [o for o in out if lo <= o[1] < hi])
print("\nhow many of the nine coins are running at 1.5x or more:")
for lo, hi in ((0, 2), (2, 5), (5, 10)):
    line(f"hot count {lo}-{hi - 1}", [o for o in out if lo <= o[2] < hi])
print("DONE")
