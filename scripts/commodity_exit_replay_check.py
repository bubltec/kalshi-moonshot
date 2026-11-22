"""Does the study's replay of commodity_exit match what the live commodity lanes really sold? (2026-11-21: scripts/binary_risk_round_trips.py closes its bets with a replay of the exit module; its numbers are only as good as that replay.)
QUESTION: on the live commodity bets since the exit module took its present form (SINCE, default 2026-11-20 00:00 ET, the day after time_to_go was added), does the replay (the same code as the round-trip study: under_even, trail with the time-aware minute, time_to_go; from the second of the buy, on the 1-second quote grid) sell the same bets, by the same step, at about the same time and price as the bot did?
Reported: per bet the real ending and the replayed one; then how many agree on sold or held, on the step, the gap in seconds and cents, and the dollars each way (the bet's own contracts, fee on both legs).
Run: SPOT=0 scripts/cloud_job.sh scripts/commodity_exit_replay_check.py [SINCE=YYYY-MM-DDTHH:MM]"""
import bisect, datetime, math, os, re
from kalshi_moonshot import config, curverisk, curvevalue, db, pricevol
from kalshi_moonshot.perpref import index_for_series as perp_index
from kalshi_moonshot.strategy import fee_per_contract as fee

ET = datetime.timezone(datetime.timedelta(hours=-5))
SINCE = datetime.datetime.strptime(os.environ.get("SINCE", "2026-11-20T00:00"), "%Y-%m-%dT%H:%M").replace(tzinfo=ET).timestamp()
s = config.reload()
st = db.connect(s.db_path, "main", shared=True)
tape = db.connect(s.tape_path, "tape", shared=True)
results = {w["ticker"]: (w["result"], float(w["close_ts"]), w["series"]) for w in st.execute("SELECT ticker, series, close_ts, result FROM windows WHERE close_ts>? AND result IN ('yes','no')", (SINCE,)).fetchall()}
rows = st.execute("SELECT ts, ticker, lane, side, stake_usd, contracts, exit_price, exit_ts, exit_reason FROM decisions WHERE lane LIKE 'commodity%' AND mode='trade' AND action='bet' AND stake_usd>0 AND contracts>0 AND ts>? ORDER BY ts", (SINCE,)).fetchall()
print(f"{len(rows)} live commodity bets since {datetime.datetime.fromtimestamp(SINCE, ET):%m-%d %H:%M} ET")
TK = {}


def ticks(ix):
    if ix not in TK:
        r = tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND ts>? ORDER BY ts", (ix, SINCE - 5000)).fetchall()
        TK[ix] = ([float(x[0]) for x in r], [float(x[1]) for x in r])
    return TK[ix]


out = []
for r in rows:
    if r["ticker"] not in results: continue
    res, close, series = results[r["ticker"]]
    side, n = r["side"], float(r["contracts"])
    entry = float(r["stake_usd"]) / n
    base = close - 960.0
    ix = perp_index(series)
    if not ix: continue
    T, Vv = ticks(ix)
    k = bisect.bisect_left(T, close - 900.0)
    if k >= len(T) or T[k] - (close - 900.0) > 60.0: continue
    strike = Vv[k]
    q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (r["ticker"], base, close)).fetchall()
    if len(q) < 20: continue
    qt = [float(x[0]) for x in q]
    scale = 100.0 if max(float(x[2]) for x in q) > 1.5 else 1.0
    yb, ya, age, j = [None] * 961, [None] * 961, [1e9] * 961, -1
    for kk in range(961):
        while j + 1 < len(q) and qt[j + 1] <= base + kk: j += 1
        if j >= 0: yb[kk], ya[kk], age[kk] = float(q[j][1]) / scale, float(q[j][2]) / scale, base + kk - qt[j]
    k0 = next((i for i in range(961) if yb[i] is not None), 961)
    kf = int(float(r["ts"]) - base)
    if k0 > kf or kf >= 950: continue
    mid = [None if yb[i] is None else (yb[i] + ya[i]) / 2.0 for i in range(961)]
    sq = [0.0] * 961
    for i in range(1, 961): sq[i] = sq[i - 1] + ((mid[i] - mid[i - 1]) ** 2 if mid[i - 1] is not None else 0.0)
    bid = lambda kk: yb[kk] if side == "yes" else 1.0 - ya[kk]
    U = {}

    def units(kk):
        kk -= kk % 5
        if kk not in U:
            m = int(min(900, kk - k0 - 1))
            stat = timed = None
            if m >= 300 and age[kk] <= 10.0:
                d = sq[kk] - sq[kk - m]
                stat = math.sqrt(d / m) * math.sqrt(60.0) if d > 0 else None
                timed = pricevol.unit_of(pricevol.implied_rate(mid[kk - m:kk + 1], close - (base + kk)))
            U[kk] = (stat, timed)
        return U[kk]

    basis, peak, under, go = entry + fee(entry), 0.0, 0.0, 0.0
    rep = (None, "settled", 960)
    for kk in range(kf + 1, 953):
        now, left, b = base + kk, 960.0 - kk, bid(kk)
        stat, timed = units(kk)
        if b >= curverisk.EVEN: under = 0.0
        elif under <= 0: under = now
        if b < curverisk.EVEN and curverisk.under_even(b, entry, now - under, left, 0.1, stat):
            rep = (b, "under_even", kk); break
        peak = max(peak, b)
        if curverisk.trail_hit(b - fee(b) - basis, fee(b), b, peak, timed or stat, 1.0):
            rep = (b, "trail", kk); break
        if b > 0 and 8.0 <= left <= 240.0:
            a = bisect.bisect_left(T, now - 1000.0); e = bisect.bisect_right(T, now)
            c = curvevalue.read(T[a:e], Vv[a:e], now, strike, side, left)
            if c is None or c.p >= 0.5: go = 0.0
            else:
                if go <= 0: go = now
                if now - go >= max(15.0, 0.25 * left):
                    rep = (b, "time_to_go", kk); break
        else:
            go = 0.0
    won = res == side
    sold = r["exit_price"] is not None and r["exit_ts"] is not None and float(r["exit_ts"]) < close - 1
    m = re.search(r"sold at (\w+)\]", r["exit_reason"] or "")
    astep = (m.group(1) if m else "other") if sold else "settled"
    aprice = float(r["exit_price"]) if sold else None
    ak = int(float(r["exit_ts"]) - base) if sold else 960
    val = lambda p: ((p - fee(p)) if p is not None else (1.0 if won else 0.0)) - basis
    out.append((r["ticker"], side, entry, (kf - 60) / 60.0, astep, aprice, ak, rep[1], rep[0], rep[2], val(aprice) * n, val(rep[0]) * n, (r["exit_reason"] or "")[:70]))

print(f"{len(out)} followed\n")
for o in out:
    print("%-28s %-3s paid %.2f min %4.1f | real %-10s %s at %4ds | replay %-10s %s at %4ds | real %+6.2f$ replay %+6.2f$ | %s" % (o[0], o[1], o[2], o[3], o[4], "%.2f" % o[5] if o[5] is not None else " -- ", o[6] - 60, o[7], "%.2f" % o[8] if o[8] is not None else " -- ", o[9] - 60, o[10], o[11], o[12] if o[4] == "other" else ""))
n = len(out)
if n:
    both = [o for o in out if o[4] != "settled" and o[7] != "settled"]
    print(f"\nagree on sold or held: {sum(1 for o in out if (o[4] == 'settled') == (o[7] == 'settled'))} of {n}; same step: {sum(1 for o in out if o[4] == o[7])} of {n}")
    print(f"real sold, replay held: {sum(1 for o in out if o[4] != 'settled' and o[7] == 'settled')}; replay sold, real held: {sum(1 for o in out if o[4] == 'settled' and o[7] != 'settled')}")
    if both:
        dt = sorted(abs(o[9] - o[6]) for o in both); dp = sorted(abs(o[8] - o[5]) for o in both)
        print(f"both sold ({len(both)}): median gap {dt[len(dt) // 2]}s, {100 * dp[len(dp) // 2]:.1f}c; within 10 s: {sum(1 for x in dt if x <= 10)}")
    steps = sorted({o[4] for o in out} | {o[7] for o in out})
    print("real step (rows) by replay step (columns): " + " ".join(steps))
    for a in steps: print("  %-11s " % a + " ".join("%4d" % sum(1 for o in out if o[4] == a and o[7] == b) for b in steps))
    print(f"dollars: real {sum(o[10] for o in out):+.2f}, replay {sum(o[11] for o in out):+.2f}")
