"""PLAY THE CURVE IN 5-SECOND CHUNKS: each side of each crypto 15-minute window is cut into CHUNK-second chunks (default 5); a chunk is up (U) if the side's ask rose more than EPS over it, down (D) if it fell more than EPS, else flat (F). At every chunk boundary in minutes 0.5-9 where the ask is 14c-42c,
the last 3 chunks (27 patterns) or the last 4 (81) name a pattern, e.g. DDU = two falls then a rise, the bottom of a curve. For each pattern the first entry per side per window is bought at the ask one second later with the buy fee, and the lane's scalp exits and sale fee are replayed second by second.
To keep a lucky pattern out, the windows are split by time: the first 4 days pick patterns (those with a positive net per dollar on at least MIN_N entries), the last 3 days test them. Reported: the best and worst patterns on the first part, what the same patterns did on the second,
and the net of "buy every pattern that worked on the first part" on the second part against buying every entry on it. Read-only.
    docker compose exec -T bot python - < scripts/chunk_curve_study.py   (SINCE_DAYS=7 EPS=0.01 CHUNK=5)
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
UP, DIP, HARD, HOLD, CASH_MIN, CASH_DROP = 0.06, 0.04, 0.09, 3, 12 * 60, 0.30
CAP = 0.50
def grid(ticker, t0, t1):
    q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (ticker, t0 - 20, t1)).fetchall()
    ts = [float(r[0]) for r in q]
    out = {}
    for sec in range(int(t0), int(t1) + 1):
        i = bisect.bisect_right(ts, sec + 0.999) - 1
        if i >= 0 and sec + 0.999 - ts[i] <= 15:
            out[sec] = (float(q[i][1]), float(q[i][2]))
    return out


def side_px(g, sec, side):
    x = g.get(sec)
    if x is None:
        return None
    return (x[0], x[1]) if side == "yes" else (1.0 - x[1], 1.0 - x[0])  # (bid, ask) in this side's terms


def exit_value(g, side, e, paid, close, won):
    """net proceeds per contract from entry second e (the buy filled at `paid`)."""
    high, since = 0.0, None
    t0 = close - 900
    for sec in range(e + 1, int(close) - 4):
        px = side_px(g, sec, side)
        if px is None:
            continue
        b = px[0]
        left, el = close - sec, sec - t0
        fire = False
        if (el >= CASH_MIN and b > paid) or (b <= paid - CASH_DROP and el >= CASH_MIN) or (left <= 5 and b < paid):
            fire = True
        if not fire:
            if b >= paid + UP - 1e-9:
                high = max(high, b)
            if high >= paid + UP - 1e-9:
                if high - b >= HARD - 1e-9:
                    fire = True
                elif b <= high - DIP + 1e-9:
                    since = sec if since is None else since
                    fire = sec - since >= HOLD
                else:
                    since = None
        if fire:
            after = side_px(g, sec + 1, side) or px
            return max(after[0], 0.0) - (fee(after[0]) if after[0] > 0 else 0.0)
    return 1.0 if won else 0.0


EPS = float(os.environ.get("EPS", "0.01"))
CHUNK = int(os.environ.get("CHUNK", "5"))
MIN_N = 120
entries = defaultdict(list)  # (k, pattern) -> [(close, net per $, armed, cost)]
wins = main.db.execute("SELECT ticker, series, close_ts, result FROM windows WHERE series IN ('KXBTC15M','KXETH15M','KXSOL15M','KXXRP15M','KXDOGE15M') AND result IN ('yes','no') AND close_ts > extract(epoch from now()) - ?*86400 ORDER BY close_ts", (days,)).fetchall()
n = 0
for w in wins:
    close = float(w["close_ts"])
    t0 = close - 900
    g = grid(w["ticker"], t0, close)
    if len(g) < 300:
        continue
    n += 1
    for side in ("yes", "no"):
        won = w["result"] == side
        seen = set()
        for sec in range(int(t0 + 30), int(t0 + 540), CHUNK):
            asks = [side_px(g, sec - CHUNK * j, side) for j in range(5)]  # now, -5, -10, -15, -20
            if any(a is None for a in asks) or not (0.14 <= asks[0][1] <= 0.42):
                continue
            ch = ["U" if asks[j][1] - asks[j + 1][1] > EPS else "D" if asks[j + 1][1] - asks[j][1] > EPS else "F" for j in range(4)]  # newest first
            for k in (3, 4):
                pat = "".join(reversed(ch[:k]))  # oldest first
                if (k, pat) in seen:
                    continue
                seen.add((k, pat))
                fill = side_px(g, sec + 1, side)
                if fill is None or not (0.05 <= fill[1] <= CAP):
                    continue
                paid = fill[1]
                cost = paid + fee(paid)
                got = exit_value(g, side, sec + 1, paid, close, won)
                peak = max([px[0] for px in (side_px(g, t, side) for t in range(sec + 1, int(close) - 4)) if px is not None] or [paid])
                entries[(k, pat)].append((close, (got - cost) / cost, peak - paid >= UP - 1e-9, cost, got - cost))
print(f"{n} windows, {days:.0f} days, chunk = {CHUNK} s, EPS = {EPS:.2f}\n")
cut = sorted({w["close_ts"] for w in wins})[int(len({w["close_ts"] for w in wins}) * 4 / 7)]


def stat(r):
    if len(r) < 20:
        return None
    by = defaultdict(float)
    for x in r:
        by[x[0]] += x[1]
    se = math.sqrt(len(by)) * statistics.pstdev(list(by.values())) / len(r) if len(by) > 1 else float("nan")
    return len(r), statistics.mean(x[1] for x in r), se, sum(x[2] for x in r) / len(r), statistics.mean(x[4] for x in r)


for k in (3, 4):
    rows = []
    for (kk, pat), r in entries.items():
        if kk != k:
            continue
        a, b = [x for x in r if x[0] < cut], [x for x in r if x[0] >= cut]
        sa, sb = stat(a), stat(b)
        if sa and sa[0] >= MIN_N:
            rows.append((pat, sa, sb))
    rows.sort(key=lambda x: -x[1][1])
    print(f"{k}-chunk patterns (U up, D down, F flat; oldest first), first part picks, second part tests; per dollar staked, armed = bid got +6c")
    print(f"  {'pattern':8} {'n 1st':>6} {'per $ 1st':>10} {'+-se':>5} {'n 2nd':>6} {'per $ 2nd':>10} {'+-se':>5} {'armed 2nd':>10}")
    for pat, sa, sb in [*rows[:6], ("...", None, None), *rows[-4:]]:
        if sa is None:
            print("  ...")
            continue
        print(f"  {pat:8} {sa[0]:>6} {100 * sa[1]:>+9.1f}% {100 * sa[2]:>4.1f}% " + (f"{sb[0]:>6} {100 * sb[1]:>+9.1f}% {100 * sb[2]:>4.1f}% {100 * sb[3]:>9.0f}%" if sb else ""))
    chosen = [pat for pat, sa, sb in rows if sa[1] > 0]
    allb = [x for (kk, pat), r in entries.items() if kk == k for x in r if x[0] >= cut]
    pick = [x for (kk, pat), r in entries.items() if kk == k and pat in chosen for x in r if x[0] >= cut]
    sp, sa_ = stat(pick), stat(allb)
    print(f"  picked {len(chosen)} patterns with a positive first part: second part {f'n={sp[0]}, {100 * sp[1]:+.1f}% per $ (+-{100 * sp[2]:.1f}%), {100 * sp[4]:+.1f}c per contract' if sp else 'none'}")
    print(f"  buying every pattern on the second part:             n={sa_[0]}, {100 * sa_[1]:+.1f}% per $ (+-{100 * sa_[2]:.1f}%), {100 * sa_[4]:+.1f}c per contract\n")
