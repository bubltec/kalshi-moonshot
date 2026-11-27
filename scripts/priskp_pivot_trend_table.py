"""Do the market's own swing highs and lows say which way the curve is going, and did the lane buy with or against that, and at a peak? (2026-11-27; user, on ETH 00:45 YES, bought 0.65 at the top of the fourth bounce of a fade from a first-minute spike: "This curve showed a down trend and shrinking curve which icnidcates it was going down. It also bought on a peak which it should never do for a favorite".)
THE READING, no length in it: the spot the lane could see at the buy (the hour before the open and the market so far, the 10 s grid of wavefacts) is cut into swings by size at shares of the biggest reversal the path shows (wavefacts.nest's way). At one size, the turns it has confirmed are highs and lows in turn. TREND: the last two highs and the last two lows: both higher = UP, both lower = DOWN, otherwise MIXED (unread with fewer than two of each). SHRINKING: the last swing is smaller than the one before it. AT A PEAK (for the bet's side): the current swing runs the bet's way and the spot is within a tenth of that swing's size of its extreme (it has not pulled back).
QUESTION (one, on the lane's own bets): every live PriskP bet since 2026-11-25 15:57 ET with tape, at the quarter, eighth and sixteenth of the biggest swing: the trend with or against the bet's side at the buy, and for the bets on the favourite side (paid over 0.5: every bet of this lane) whether it was bought at a peak: n, how often the market settled the bet's way, the price paid, held to settlement, as run, by day.
HONEST LIMITS: the sizes and the definitions were stated before the run; the idea is the user's reading of one bet; a day and a half; no asset key. A lead for the curve reading, scored forward.
Run: PYTHONPATH=src .venv/bin/python scripts/priskp_pivot_trend_table.py BETS.json.gz INDEX.json.gz"""
import datetime
import gzip
import json
import math
import re
import statistics
import sys

from kalshi_moonshot import ticks, wavefacts
from kalshi_moonshot.kalshiws import index_for_series

ET = datetime.timezone(datetime.timedelta(hours=-5))
ticks.history.keep = ticks.history.slack = 1e12
for ix, (T, V) in json.load(gzip.open(sys.argv[2])).items():
    for t, v in zip(T, V, strict=True):
        ticks.history.note(ix, float(t), float(v))
fee = lambda p: 0.07 * p * (1.0 - p)  # noqa: E731
SHARES = (0.25, 0.125, 0.0625)


def pivots(g: list[float], rev: float) -> tuple[list[tuple[float, int]], int, float]:
    """(confirmed turns [(value, +1 high / -1 low)], the current direction, the running extreme) of a zig-zag of reversal `rev`."""
    out: list[tuple[float, int]] = []
    d, lo, hi, ev = 0, g[0], g[0], g[0]
    for v in g[1:]:
        if d == 0:
            lo, hi = min(lo, v), max(hi, v)
            if v - lo >= rev:
                out.append((lo, -1))
                d, ev = 1, v
            elif hi - v >= rev:
                out.append((hi, 1))
                d, ev = -1, v
        elif (v - ev) * d > 0:
            ev = v
        elif (ev - v) * d >= rev:
            out.append((ev, d))
            d, ev = -d, v
    return out, d, ev


def read(g: list[float], rev: float, want: int) -> dict | None:
    p, d, ev = pivots(g, rev)
    highs = [v for v, k in p if k > 0]
    lows = [v for v, k in p if k < 0]
    if d == 0 or len(highs) < 2 or len(lows) < 2:
        return None
    up, down = highs[-1] > highs[-2] and lows[-1] > lows[-2], highs[-1] < highs[-2] and lows[-1] < lows[-2]
    trend = 1 if up else -1 if down else 0
    sizes = [abs(b[0] - a[0]) for a, b in zip(p, p[1:], strict=False)]
    start = p[-1][0]
    size = abs(ev - start)
    return {"trend": "WITH" if trend * want > 0 else "AGAINST" if trend * want < 0 else "MIXED", "shrinking": len(sizes) >= 2 and sizes[-1] < sizes[-2], "peak": d == want and size > 0 and abs(g[-1] - ev) <= 0.1 * size, "riding": d == want}


rows = {s: [] for s in SHARES}
for b in json.load(gzip.open(sys.argv[1])):
    if len(b["bid"]) != 900 or not b["strike"] or b["result"] not in ("yes", "no"):
        continue
    ix = index_for_series(re.match(r"(KX[A-Z0-9]+15M)", b["ticker"]).group(1))
    opened, now = float(b["close"] - 900), float(math.ceil(b["ts"]))
    got = ticks.history.window(ix, opened - wavefacts.HOUR - wavefacts.GRID, now)
    if not got or not got[0]:
        continue
    g = wavefacts._grid(got[0], got[1], opened - wavefacts.HOUR, now)
    if len(g) < 300:
        continue
    lo, hi = 0.0, max(g) - min(g)
    for _ in range(30):
        mid = (lo + hi) / 2.0
        if wavefacts._swing_dir(g, mid)[0] >= 2:
            lo = mid
        else:
            hi = mid
    if lo <= 0:
        continue
    want = 1 if b["side"] == "yes" else -1
    won = b["result"] == b["side"]
    base = {"won": won, "paid": b["entry"], "held": ((1.0 if won else 0.0) - b["entry"]) * b["n"], "run": (((b["exit_price"] - fee(b["exit_price"])) if b["exit_ts"] else (1.0 if won else 0.0)) - b["entry"]) * b["n"], "day": datetime.datetime.fromtimestamp(b["ts"], ET).strftime("%m-%d")}
    for s in SHARES:
        r = read(g, lo * s, want)
        rows[s].append({**base, "r": r})


def line(name: str, v: list) -> str:
    days = {d: round(sum(x["held"] for x in v if x["day"] == d), 2) for d in sorted({x["day"] for x in v})}
    return f"   {name:44s} n {len(v):3d}  settled its way {sum(x['won'] for x in v) / len(v):4.0%}  avg paid {statistics.mean(x['paid'] for x in v):.2f}  held ${sum(x['held'] for x in v):8.2f} ({sum(x['held'] for x in v) / len(v):+.2f} a bet)  as run ${sum(x['run'] for x in v):8.2f}  by day {days}"


for s in SHARES:
    v = rows[s]
    print(f"\nswings of 1/{round(1 / s)} of the market's biggest ({len(v)} bets):")
    for k in ("WITH", "MIXED", "AGAINST"):
        w = [x for x in v if x["r"] and x["r"]["trend"] == k]
        if w:
            print(line(f"the highs and lows trend {k} the bet", w))
    w = [x for x in v if x["r"] and x["r"]["trend"] == "AGAINST" and x["r"]["shrinking"]]
    if w:
        print(line("  ... against, and the swings are shrinking", w))
    w = [x for x in v if x["r"] is None]
    if w:
        print(line("unread (fewer than two highs and two lows)", w))
    for k, name in ((True, "bought AT A PEAK of the bet's own swing"), (False, "bought off the peak (pulled back, or against)")):
        w = [x for x in v if x["r"] and x["r"]["peak"] == k]
        if w:
            print(line(name, w))
