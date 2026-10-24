"""Is a quick jump in a contract's own price, over a few seconds as a share of its price, a good moment to buy it? Replays ticks; places nothing.

On a one-second grid for each window and side, a jump is the side's mid price moving by at least `J` of its level over `W` seconds (up, or down for the
dip version). We buy at the ask one second later (fees both ways) and measure what follows: the return selling at the bid after 10 / 30 / 60 / 120 s, holding to
settlement, and under the live crash exit. The control is the same measurement at random moments in the same markets, matched on price band and minute range.
    docker compose exec -T bot python - < scripts/jump_study.py
"""
import bisect
import math
import random
from collections import defaultdict

from kalshi_moonshot import config, db, grid
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee

s = config.reload()
st = Store(s.db_path, s.exclude_versions)
tape = db.connect(s.tape_path, "tape", shared=True)
SER = ("KXGOLD15M", "KXSILVER15M", "KXWTI15M", "KXNATGAS15M", "KXCOPPER15M", "KXPLATINUM15M", "KXPALLADIUM15M", "KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M", "KXDOGE15M",
       "KXEURUSD15M", "KXGBPUSD15M", "KXUSDJPY15M", "KXAUDUSD15M", "KXUSDCAD15M")
wins = st.db.execute("select ticker, close_ts, result from windows where result in ('yes','no') and series in (%s)" % ",".join("'%s'" % x for x in SER)).fetchall()
HOLDS = (10, 30, 60, 120)
CUSH = 0.01
random.seed(2)


def outcome(path, times, i, result, side):
    """returns (dict of returns by measure) for a buy at path[i] ask."""
    t0, _b0, ask = path[i]
    cost = ask + fee(ask)
    out = {}
    for h in HOLDS:
        j = bisect.bisect_right(times, t0 + h) - 1
        if j <= i or times[j] > path[-1][0] or t0 + h > path[-1][0] - 5:
            out[f"sell {h}s"] = None
            continue
        b = path[j][1] - CUSH
        out[f"sell {h}s"] = (b - fee(max(b, 0))) / cost - 1
    out["to settlement"] = (1.0 if result == side else 0.0) / cost - 1
    sold = grid.trail_exit(path[i + 1:], ask)
    out["live crash exit"] = grid._roi_exit(ask, sold) / 1.0 if sold is not None else out["to settlement"]
    out["bounce +8c in 2 min"] = None
    j2 = bisect.bisect_right(times, t0 + 120) - 1
    if j2 > i:
        out["bounce +8c in 2 min"] = 1.0 if max(p[1] for p in path[i + 1:j2 + 1]) >= ask + 0.08 else 0.0
    return ask, out


def collect(W, J, direction):
    ev, ctl = [], []
    for r in wins:
        close = float(r["close_ts"]); start = close - 900
        q = tape.execute("select ts,bid,ask from quotes where ticker=? and bid is not null and ask is not null order by ts", (r["ticker"],)).fetchall()
        if not q or q[0][0] > start + 120:
            continue
        for side in ("yes", "no"):
            path = [(t, b if side == "yes" else 1 - a, a if side == "yes" else 1 - b) for t, b, a in q if start <= t < close - 5]
            if len(path) < 60:
                continue
            times = [p[0] for p in path]
            mid = [(p[1] + p[2]) / 2 for p in path]
            last_event = -1e9
            cand_ctl = []
            for i in range(len(path)):
                t = times[i]
                m = (t - start) / 60
                if m < 1.5 or m > 12:
                    continue
                k = bisect.bisect_right(times, t - W) - 1
                if k < 0 or t - times[k] > W + 2:
                    continue
                base = mid[k]
                if not (0.1 <= base <= 0.9) or not (0.1 <= path[i][2] <= 0.9):
                    continue
                move = mid[i] / base - 1
                ok = move >= J if direction == "up" else move <= -J
                # enter one second later
                e = bisect.bisect_right(times, t + 1) - 1
                if e <= i or e >= len(path) - 3:
                    continue
                if ok and t - last_event >= 30:
                    last_event = t
                    ev.append((r["ticker"], side, m, *outcome(path, times, e, r["result"], side)))
                elif not ok:
                    cand_ctl.append(e)
            for e in random.sample(cand_ctl, min(3, len(cand_ctl))):
                if 0.1 <= path[e][2] <= 0.9:
                    ctl.append((r["ticker"], side, (times[e] - start) / 60, *outcome(path, times, e, r["result"], side)))
    return ev, ctl


def summarize(rows, key):
    by = defaultdict(list)
    for tk, sd, _m, ask, out in rows:
        v = out.get(key)
        if v is not None:
            by[(tk, sd)].append(v)
    means = [sum(v) / len(v) for v in by.values()]
    if len(means) < 5:
        return None
    n = sum(len(v) for v in by.values())
    m = sum(sum(v) for v in by.values()) / n
    se = math.sqrt(sum((x - sum(means) / len(means)) ** 2 for x in means) / (len(means) - 1) / len(means))
    return n, m, se


print(f"{len(wins)} windows. Returns are per contract bought, after the spread and fees (buy the ask one second after the jump).\n")
for direction, label in (("up", "the price JUMPED UP (buy it: momentum)"), ("down", "the price DROPPED (buy it: the dip)")):
    for W in (4,):
        for J in (0.05, 0.10, 0.20):
            ev, ctl = collect(W, J, direction)
            print(f"== {label}: {J:.0%} or more of its price within {W} s  ->  {len(ev)} events, {len({(e[0], e[1]) for e in ev})} windows (control: {len(ctl)} random moments)")
            print(f"   {'after the buy':24}{'EVENT':>26}{'CONTROL':>26}")
            for key in ["sell 10s", "sell 30s", "sell 60s", "sell 120s", "live crash exit", "to settlement", "bounce +8c in 2 min"]:
                a, b = summarize(ev, key), summarize(ctl, key)
                fa = f"{a[1]:+8.1%} ±{a[2]:5.1%} (n={a[0]})" if a else "-"
                fb = f"{b[1]:+8.1%} ±{b[2]:5.1%} (n={b[0]})" if b else "-"
                print(f"   {key:24}{fa:>26}{fb:>26}")
            print()
