"""How much of the move against it had the bet's swing taken back when the lane bought? (2026-11-27; user, on six NO bets bought within 50 s at 00:17 against a rise of 2.5 to 8 typical minutes, all lost: "Why is it buying in a small dip? Just seems like a bad idea with low cushion to buy in a curve like that. Would have been better to wait and it would have picked a different place to bet".)
THE READING, no length in it: the spot the lane could see at the buy (the hour before the open and the market so far, the 10 s grid of wavefacts) is cut into swings by size at 1, 1/2, 1/4 and 1/8 of the biggest reversal the path shows (wavefacts.nest's levels). Going up from the smallest level, the first level whose current swing runs AGAINST the bet is the move the bet is betting against. TAKEN BACK is how far the spot has come back from that move's extreme, as a share of that move's size: near 0 = the bet was bought at the very start of a dip or bounce inside the move; near 1 = the move has been all but undone. A bet no level runs against has nothing to take back (the move is the bet's own at every size).
QUESTION (one, on the lane's own bets): every live PriskP bet since 2026-11-25 15:57 ET with tape, by the share taken back at the buy (quarters), and beside it by the model's own chance at the buy where the buy note has it (the user's "low cushion"): n, how often the market settled the bet's way, the price paid, held to settlement, as run, by day.
HONEST LIMITS: the quarters were stated before the run; the idea came from one round of six bets; a day and a half; no asset key. A lead for the curve reading (a wait, not a refusal), scored forward.
Run: PYTHONPATH=src .venv/bin/python scripts/priskp_pullback_share_table.py BETS.json.gz INDEX.json.gz [ROWS.txt from priskp_entry_readings_table.py for the chance]"""
import collections
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
chance = {}
if len(sys.argv) > 3:
    for line in open(sys.argv[3]):
        f = line.rstrip("\n").split("|")
        if len(f) >= 8 and f[0].startswith("KX"):
            m = re.search(r"chance: ([\d\.]+)", f[7])
            if m:
                chance[(f[0], round(float(f[2])))] = float(m.group(1))


def swing(g: list[float], rev: float) -> tuple[int, float, float]:
    """(direction, the current swing's start value, its extreme) of a zig-zag of reversal `rev`; direction 0 before the first swing."""
    d, lo, hi, ev, start = 0, g[0], g[0], g[0], g[0]
    for v in g[1:]:
        if d == 0:
            lo, hi = min(lo, v), max(hi, v)
            if v - lo >= rev:
                d, start, ev = 1, lo, v
            elif hi - v >= rev:
                d, start, ev = -1, hi, v
        elif (v - ev) * d > 0:
            ev = v
        elif (ev - v) * d >= rev:
            d, start, ev = -d, ev, v
    return d, start, ev


def taken_back(index: str, opened: float, now: float, side: str) -> tuple[float | None, int] | None:
    """(the share taken back, or None when no level runs against the bet; the level it was read at, 0 = the smallest) or None when unread."""
    got = ticks.history.window(index, opened - wavefacts.HOUR - wavefacts.GRID, now)
    if not got or not got[0]:
        return None
    g = wavefacts._grid(got[0], got[1], opened - wavefacts.HOUR, now)
    if len(g) < 300:
        return None
    lo, hi = 0.0, max(g) - min(g)
    for _ in range(30):
        mid = (lo + hi) / 2.0
        if wavefacts._swing_dir(g, mid)[0] >= 2:
            lo = mid
        else:
            hi = mid
    if lo <= 0:
        return None
    want = 1 if side == "yes" else -1
    for n, s in enumerate(reversed(wavefacts.NEST_LEVELS)):
        d, start, ev = swing(g, lo * s * 0.999)
        if d == -want and abs(ev - start) > 0:
            return abs(g[-1] - ev) / abs(ev - start), n
    return None, -1


rows = []
for b in json.load(gzip.open(sys.argv[1])):
    if len(b["bid"]) != 900 or not b["strike"] or b["result"] not in ("yes", "no"):
        continue
    ix = index_for_series(re.match(r"(KX[A-Z0-9]+15M)", b["ticker"]).group(1))
    got = taken_back(ix, float(b["close"] - 900), float(math.ceil(b["ts"])), b["side"])
    if got is None:
        continue
    won = b["result"] == b["side"]
    rows.append({"share": got[0], "level": got[1], "won": won, "paid": b["entry"], "held": ((1.0 if won else 0.0) - b["entry"]) * b["n"], "run": (((b["exit_price"] - fee(b["exit_price"])) if b["exit_ts"] else (1.0 if won else 0.0)) - b["entry"]) * b["n"], "day": datetime.datetime.fromtimestamp(b["ts"], ET).strftime("%m-%d"), "chance": chance.get((b["ticker"], round(b["ts"])))})


def line(name: str, v: list) -> str:
    days = {d: round(sum(x["held"] for x in v if x["day"] == d), 2) for d in sorted({x["day"] for x in v})}
    return f"   {name:44s} n {len(v):3d}  settled its way {sum(x['won'] for x in v) / len(v):4.0%}  avg paid {statistics.mean(x['paid'] for x in v):.2f}  held ${sum(x['held'] for x in v):8.2f} ({sum(x['held'] for x in v) / len(v):+.2f} a bet)  as run ${sum(x['run'] for x in v):8.2f}  by day {days}"


BINS = (("under a quarter taken back", lambda s: s is not None and s < 0.25), ("a quarter to a half", lambda s: s is not None and 0.25 <= s < 0.5), ("a half to three quarters", lambda s: s is not None and 0.5 <= s < 0.75), ("three quarters and more", lambda s: s is not None and s >= 0.75), ("nothing against the bet at any size", lambda s: s is None))
print(f"{len(rows)} bets\n\nby the share of the move against it that the bet's swing had taken back at the buy:")
for name, f in BINS:
    v = [r for r in rows if f(r["share"])]
    if v:
        print(line(name, v))
if chance:
    print("\nthe same, split by the model's own chance at the buy (the buy note's `chance`):")
    for cname, cf in (("chance under 0.6", lambda c: c is not None and c < 0.6), ("chance 0.6 and over", lambda c: c is not None and c >= 0.6)):
        for name, f in (("under a half taken back", lambda s: s is not None and s < 0.5), ("a half and more, or nothing against", lambda s: s is None or s >= 0.5)):
            v = [r for r in rows if cf(r["chance"]) and f(r["share"])]
            if v:
                print(line(f"{cname}, {name}", v))
