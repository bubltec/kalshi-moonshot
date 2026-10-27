"""IS THE FIRST HALF OF A 15-MINUTE MARKET A DIFFERENT MARKET: every finished window on the tape (crypto, currencies, metals and energy), favorites bought at 0.76-0.92 and held to the result, split by when in the window the buy happened (2.5-minute buckets),
plus how reliable the leader is at each point (the side priced over 50c at the start of each bucket: how often it is the one that wins, by its price), and how much the price moves per minute early against late.
Then the two-plays idea: play one buys the first 0.76-0.92 side in minutes 2-7.5 and sells it at the 7.5 mark (or the first bid 8c up); play two buys the first 0.76-0.92 side in minutes 7.5-12 and holds. Per dollar staked after the buy fee, errors clustered by window.
Read-only. Places nothing.
    docker compose exec -T bot python - < scripts/half_market_study.py
"""
import bisect
import math
import statistics
from collections import defaultdict

from kalshi_moonshot import config, db
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee

s = config.reload()
tape = db.connect(s.tape_path, "tape", shared=True)
main = Store(s.db_path, s.exclude_versions)
LO, HI = 0.76, 0.92
BUCKETS = ((120, 300), (300, 450), (450, 600), (600, 750))  # seconds after the open


def family(series):
    if series[2:].startswith(("BTC", "ETH", "SOL", "XRP", "DOGE")):
        return "crypto"
    return "fx" if any(x in series for x in ("EUR", "GBP", "JPY", "AUD", "CAD")) else "commodity"


wins = []
for w in main.db.execute("SELECT ticker, series, close_ts, result FROM windows WHERE series LIKE '%15M' AND result IN ('yes','no')").fetchall():
    T = float(w["close_ts"])
    q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (w["ticker"], T - 900, T)).fetchall()
    if len(q) < 80 or q[0][0] > T - 840:
        continue  # the tape must cover the window from near its open
    wins.append({"T": T, "res": w["result"], "q": q, "ts": [x[0] for x in q], "fam": family(w["series"])})
print(len(wins), "finished windows with the tape from their open")


def px(q, side):  # (ts, side bid, side ask)
    return (q[0], q[1] if side == "yes" else 1 - q[2], q[2] if side == "yes" else 1 - q[1])


def first_entry(win, side, lo_s, hi_s):
    for x in win["q"]:
        e = x[0] - (win["T"] - 900)
        if lo_s <= e < hi_s:
            _, b, a = px(x, side)
            if LO <= a <= HI:
                return x[0], a
    return None


def roi_hold(win, side, a):
    won = win["res"] == side
    return ((1.0 if won else 0.0) - a - fee(a)) / (a + fee(a))


def cluster(rows):  # rows: (window T, roi)
    by = defaultdict(list)
    for t, r in rows:
        by[t].append(r)
    m = statistics.mean(r for _, r in rows)
    sums = [sum(v) for v in by.values()]
    se = math.sqrt(len(sums)) * statistics.pstdev(sums) / len(rows) if len(sums) > 1 else float("nan")
    return m, se


print("\nfavorite bought at 0.76-0.92 and held, by when in the window it was bought (first such quote per side per bucket)")
for fam in (None, "crypto", "fx", "commodity"):
    print(" ", fam or "all")
    for lo_s, hi_s in BUCKETS:
        rows, won = [], 0
        for win in wins:
            if fam and win["fam"] != fam:
                continue
            for side in ("yes", "no"):
                e = first_entry(win, side, lo_s, hi_s)
                if e:
                    rows.append((win["T"], roi_hold(win, side, e[1])))
                    won += win["res"] == side
        if len(rows) >= 30:
            m, se = cluster(rows)
            print(f"    minute {lo_s / 60:4.1f}-{hi_s / 60:4.1f}: {len(rows):4} bets, {100 * won / len(rows):3.0f}% won, ROI {m:+6.1%} +-{se:.1%}")

print("\nthe leader at the start of each bucket (side over 50c): how often it wins, by its price")
for lo_s, hi_s in BUCKETS:
    bands = defaultdict(lambda: [0, 0])
    for win in wins:
        i = bisect.bisect_left(win["ts"], win["T"] - 900 + lo_s)
        if i >= len(win["q"]):
            continue
        x = win["q"][i]
        mid = (x[1] + x[2]) / 2
        side = "yes" if mid >= 0.5 else "no"
        p = mid if side == "yes" else 1 - mid
        band = "0.5-0.65" if p < 0.65 else "0.65-0.8" if p < 0.8 else "0.8-0.9" if p < 0.9 else "0.9+"
        bands[band][0] += 1
        bands[band][1] += win["res"] == side
    print(f"  from minute {lo_s / 60:4.1f}: " + "  ".join(f"{b}: {100 * v[1] / v[0]:3.0f}% of {v[0]}" for b, v in sorted(bands.items())))

print("\nprice movement: mean absolute change of the mid per minute")
for lo_m, hi_m in ((1, 4), (4, 7.5), (7.5, 11), (11, 14)):
    ch = []
    for win in wins:
        a = bisect.bisect_left(win["ts"], win["T"] - 900 + lo_m * 60)
        b = bisect.bisect_left(win["ts"], win["T"] - 900 + hi_m * 60)
        if 0 < a < b < len(win["q"]):
            ma, mb = (win["q"][a][1] + win["q"][a][2]) / 2, (win["q"][b][1] + win["q"][b][2]) / 2
            ch.append(abs(mb - ma) / (hi_m - lo_m))
    print(f"  minutes {lo_m:4}-{hi_m:4}: {statistics.mean(ch):.3f} per minute ({len(ch)} windows)")

print("\ntwo plays per window: play one in minutes 2-7.5 sold at the 7.5 mark (or 8c up), play two in minutes 7.5-12 held")
p1, p2, both, single = [], [], [], []
for win in wins:
    tot = 0.0
    got1 = got2 = None
    for side in ("yes", "no"):
        e = first_entry(win, side, 120, 450)
        if e:
            t0, a = e
            sold = None
            for x in win["q"]:
                if x[0] <= t0:
                    continue
                el = x[0] - (win["T"] - 900)
                _, b, _a = px(x, side)
                if b >= a + 0.08 or el >= 450:
                    sold = b
                    break
            if sold is not None:
                cost = a + fee(a)
                r = (sold - fee(sold) - cost) / cost
                p1.append((win["T"], r))
                got1 = r
                break
    for side in ("yes", "no"):
        e = first_entry(win, side, 450, 720)
        if e:
            r = roi_hold(win, side, e[1])
            p2.append((win["T"], r))
            got2 = r
            break
    for side in ("yes", "no"):
        e = first_entry(win, side, 300, 720)
        if e:
            single.append((win["T"], roi_hold(win, side, e[1])))
            break
    if got1 is not None and got2 is not None:
        both.append((win["T"], got1 + got2))
for name, rows in (("play one (sold at 7.5 or +8c)", p1), ("play two (held)", p2), ("one play, minutes 5-12, held", single)):
    if len(rows) >= 30:
        m, se = cluster(rows)
        print(f"  {name:32} {len(rows):4} bets, ROI {m:+6.1%} +-{se:.1%}")
if len(both) >= 30:
    m, se = cluster(both)
    print(f"  windows where both plays happened: {len(both)}, sum of the two ROIs per window {m:+6.1%} +-{se:.1%}")
