"""IS A HIGH MODEL CONFIDENCE ON A CHEAP CONTRACT REAL: a friend's rule table buys 15-minute contracts priced 5c-35c when "model confidence" is 75% (12-9 min left), 90% (9-6 left) or 92.5% (6-3 left). Replayed on every Coinbase-priced crypto window on the tape.
The model here is ours, not theirs: the chance this side finishes ahead = the normal CDF of (spot - strike) in this side's direction over the window's own noise (standard deviation of 1 s spot moves in the last 5 minutes times the square root of the seconds left).
Per time band, the first moment each side of each window has model confidence at or over the bar AND an ask inside the price band (checked every 5 s), bought at the ask one second later with the buy fee, held to the result.
Reported: entries, the model's average confidence, the actual win rate, the average ask paid, and net per dollar staked with a window-clustered standard error. Then the model's calibration over every moment on the grid (confidence bucket against actual wins).
A confidence over 90% on an ask of 35c or under means the model claims the market is wrong by 55 points; this shows who is right. Read-only.
    docker compose exec -T bot python - < scripts/confidence_vs_ask_study.py   (SINCE_DAYS=7)
"""
import bisect
import math
import os
import statistics
from collections import defaultdict

from kalshi_moonshot import config, db
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee

s = config.reload()
tape = db.connect(s.tape_path, "tape", shared=True)
main = Store(s.db_path, s.exclude_versions)
days = float(os.environ.get("SINCE_DAYS", "7"))
COIN = {"KXBTC15M": "coinbase:BTC-USD", "KXETH15M": "coinbase:ETH-USD", "KXSOL15M": "coinbase:SOL-USD", "KXXRP15M": "coinbase:XRP-USD", "KXDOGE15M": "coinbase:DOGE-USD"}
BANDS = [("12-9 min left, conf >= 75%", 540, 720, 0.75), ("9-6 min left, conf >= 90%", 360, 540, 0.90), ("6-3 min left, conf >= 92.5%", 180, 360, 0.925)]
PRICES = [("5-20c", 0.05, 0.20), ("15-35c", 0.15, 0.35), ("any ask under 50c", 0.05, 0.50)]


def cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def series(ticker, table, t0, t1, key, col, ok_age):
    q = tape.execute(f"SELECT ts, {col} FROM {table} WHERE {key}=? AND ts BETWEEN ? AND ? ORDER BY ts", (ticker, t0, t1)).fetchall()
    return [float(r[0]) for r in q], q


events = defaultdict(list)  # (band, price band) -> [(window, ask, net per $, conf, won)]
calib = defaultdict(lambda: [0, 0])  # confidence bucket -> [n, wins]
wins = main.db.execute("SELECT ticker, series, close_ts, strike, result FROM windows WHERE series IN ('KXBTC15M','KXETH15M','KXSOL15M','KXXRP15M','KXDOGE15M') AND result IN ('yes','no') AND strike > 0 AND close_ts > extract(epoch from now()) - ?*86400", (days,)).fetchall()
n = 0
for w in wins:
    close, strike = float(w["close_ts"]), float(w["strike"])
    t0 = close - 900
    sq = tape.execute("SELECT ts, px FROM feed_ticks WHERE symbol=? AND ts BETWEEN ? AND ? ORDER BY ts", (COIN[w["series"]], t0 - 330, close)).fetchall()
    qq = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (w["ticker"], t0, close)).fetchall()
    if len(sq) < 150 or len(qq) < 300:
        continue
    n += 1
    sts, qts = [float(r[0]) for r in sq], [float(r[0]) for r in qq]

    def spot_at(sec, sts=sts, sq=sq):
        i = bisect.bisect_right(sts, sec) - 1
        return float(sq[i][1]) if i >= 0 and sec - sts[i] <= 10 else None

    def quote_at(sec, qts=qts, qq=qq):
        i = bisect.bisect_right(qts, sec) - 1
        return (float(qq[i][1]), float(qq[i][2])) if i >= 0 and sec - qts[i] <= 15 else None
    taken = set()
    for sec in range(int(t0) + 180, int(close) - 179, 5):
        left = close - sec
        sp = spot_at(sec)
        moves = [b - a for a, b in ((spot_at(k - 1), spot_at(k)) for k in range(sec - 300, sec + 1, 3)) if a is not None and b is not None]
        if sp is None or len(moves) < 20:
            continue
        sig = statistics.pstdev(moves) / math.sqrt(3.0)
        if sig <= 0:
            continue
        p_yes = cdf((sp - strike) / (sig * math.sqrt(left)))
        q = quote_at(sec)
        fq = quote_at(sec + 1)
        if q is None or fq is None:
            continue
        for side, p in (("yes", p_yes), ("no", 1.0 - p_yes)):
            won = w["result"] == side
            b = min(int(p * 10), 9)
            if sec % 30 == 0:
                calib[b][0] += 1
                calib[b][1] += won
            for bname, lo_left, hi_left, bar in BANDS:
                if not (lo_left <= left < hi_left) or p < bar:
                    continue
                paid = fq[1] if side == "yes" else 1.0 - fq[0]
                ask = q[1] if side == "yes" else 1.0 - q[0]
                for pname, plo, phi in PRICES:
                    if plo <= ask <= phi and plo <= paid <= phi + 0.02 and (bname, pname, side) not in taken:
                        taken.add((bname, pname, side))
                        net = ((1.0 if won else 0.0) - paid - fee(paid)) / (paid + fee(paid))
                        events[(bname, pname)].append((close, paid, net, p, won))
print(f"{n} windows, {days:.0f} days (Coinbase spot; our model, not the friend's)\n")
print(f"{'time band':32} {'ask band':18} {'n':>5} {'avg conf':>9} {'wins':>6} {'avg paid':>9} {'net per $':>10} {'+-se':>6}")
for bname, *_ in BANDS:
    for pname, *_ in PRICES:
        r = events.get((bname, pname), [])
        if len(r) < 15:
            print(f"{bname:32} {pname:18} {len(r):>5}")
            continue
        by = defaultdict(float)
        for x in r:
            by[x[0]] += x[2]
        se = math.sqrt(len(by)) * statistics.pstdev(list(by.values())) / len(r) if len(by) > 1 else float("nan")
        print(f"{bname:32} {pname:18} {len(r):>5} {100 * statistics.mean(x[3] for x in r):>8.0f}% {100 * sum(x[4] for x in r) / len(r):>5.0f}% {statistics.mean(x[1] for x in r):>9.2f} {100 * statistics.mean(x[2] for x in r):>+9.1f}% {100 * se:>5.1f}%")
print("\nthe model's calibration, every side and moment (30 s grid, minutes 3-12): confidence bucket against actual wins")
for b in sorted(calib):
    nn, ww = calib[b]
    print(f"  model {10 * b:>3}-{10 * b + 10:<3}%  n={nn:>6}  actual {100 * ww / nn:5.1f}%")
