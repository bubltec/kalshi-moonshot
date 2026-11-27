"""Was the bet bought with or against the market's OWN swings, whatever their length? (2026-11-26; user, on the table that split bets by the 3 and 10 minute travel: "Don't build on waves that are specific lengths. There could by multiple waves in 3 mins, or one wave in 15".)
THE READING, with no length in it: the spot's path the lane could see at the buy (the hour before the open and the market so far; that hour is how far back it looks, not a wave's length) is cut into swings by a zig-zag at a reversal size R. As R grows the swings merge; BIGGEST is the largest R at which the path still shows a turn (two swings): the biggest wave this market has made in what the lane can see. Every other size is a share of that one: a half, a quarter, an eighth. A swing's time is whatever it took (printed below, so the spread can be seen).
QUESTION (one, on the lane's own bets): every live PriskP bet since 2026-11-25 15:57 ET with tape, split by whether its side ran WITH or AGAINST the current swing at each share of the market's biggest swing, at the moment of the buy: n, how often the market settled the bet's way, the price paid, the result held to settlement and as run, and by day. The buys rested (no buy fee).
HONEST LIMITS: the shares (1, 1/2, 1/4, 1/8) were stated before the run; the idea of splitting by with/against came from an earlier table that used fixed lengths; a day and a half of bets; no asset key. A lead for one stated entry rule scored forward.
Run: PYTHONPATH=src .venv/bin/python scripts/priskp_own_swings_at_buy_table.py BETS.json.gz INDEX.json.gz"""
import datetime
import math
import pathlib
import re
import statistics
import sys

src = pathlib.Path(__file__).with_name("priskp_swing_scale_study.py").read_text().split("NAMES = [")[0]  # the readers only: the tape, path(), fee()
ns: dict = {"__name__": "scale_readers"}
exec(compile(src, "scale_readers", "exec"), ns)
bets, path, fee, index_for_series, waveturn = ns["bets"], ns["path"], ns["fee"], ns["index_for_series"], ns["waveturn"]
ET = datetime.timezone(datetime.timedelta(hours=-5))
SHARES = (1.0, 0.5, 0.25, 0.125)


def swings(xs: list[float], rev: float) -> tuple[int, int, int]:
    """(turns confirmed, the current direction, seconds since the current swing began) of the zig-zag at `rev`."""
    d, lo, hi, klo, khi, ek, ev, turns, start = 0, xs[0], xs[0], 0, 0, 0, xs[0], 0, 0
    for k in range(1, len(xs)):
        v = xs[k]
        if d == 0:
            if v < lo:
                lo, klo = v, k
            if v > hi:
                hi, khi = v, k
            if v - lo >= rev:
                d, ek, ev, start, turns = 1, k, v, klo, turns + 1
            elif hi - v >= rev:
                d, ek, ev, start, turns = -1, k, v, khi, turns + 1
        elif (v - ev) * d > 0:
            ek, ev = k, v
        elif (ev - v) * d >= rev:
            d, start, turns = -d, ek, turns + 1
            ek, ev = k, v
    return turns, d, len(xs) - 1 - start


def biggest(xs: list[float]) -> float:
    """The largest reversal size at which the path still shows two swings (a turn after its first swing)."""
    lo, hi = 0.0, max(xs) - min(xs)
    for _ in range(30):
        mid = (lo + hi) / 2.0
        if swings(xs, mid)[0] >= 2:
            lo = mid
        else:
            hi = mid
    return lo


rows = {s: {"WITH": [], "AGAINST": []} for s in SHARES}
big_u, ages = [], {s: [] for s in SHARES}
for b in bets:
    close, side, entry, strike = b["close"], b["side"], b["entry"], b["strike"]
    if len(b["bid"]) != 900 or not strike or b["result"] not in ("yes", "no"):
        continue
    ix = index_for_series(re.match(r"(KX[A-Z0-9]+15M)", b["ticker"]).group(1))
    opened = close - 900
    xs = path(ix, opened - 3600, int(math.ceil(b["ts"])))
    u = waveturn._hour(ix, opened).u
    if xs is None or not u or len(xs) < 600:
        continue
    R = biggest(xs)
    if R <= 0:
        continue
    big_u.append(R / u)
    want = 1 if side == "yes" else -1
    won = b["result"] == side
    run = ((b["exit_price"] - fee(b["exit_price"])) if b["exit_ts"] else (1.0 if won else 0.0)) - entry
    day = datetime.datetime.fromtimestamp(b["ts"], ET).strftime("%m-%d")
    for s in SHARES:
        _t, d, age = swings(xs, R * s * 0.999)
        if d == 0:
            continue
        ages[s].append(age)
        rows[s]["WITH" if d * want > 0 else "AGAINST"].append((won, entry, ((1.0 if won else 0.0) - entry) * b["n"], run * b["n"], day))
q = lambda v, p: sorted(v)[int(p * (len(v) - 1))]  # noqa: E731
print(f"{len(big_u)} bets. The market's biggest swing in what the lane could see, in typical minutes: median {statistics.median(big_u):.1f}, from {min(big_u):.1f} to {max(big_u):.1f}")
for s in SHARES:
    a = ages[s]
    print(f"\nswings of {s:g} x the biggest: the current swing had run a median {statistics.median(a) / 60:.1f} minutes at the buy (a tenth under {q(a, 0.1) / 60:.1f}, a tenth over {q(a, 0.9) / 60:.1f})")
    for k in ("WITH", "AGAINST"):
        v = rows[s][k]
        if v:
            days = {d: round(sum(x[2] for x in v if x[4] == d), 2) for d in sorted({x[4] for x in v})}
            print(f"   bought {k:8s} n {len(v):3d}  settled its way {sum(x[0] for x in v) / len(v):4.0%}  avg paid {statistics.mean(x[1] for x in v):.2f}  held ${sum(x[2] for x in v):8.2f} ({sum(x[2] for x in v) / len(v):+.2f} a bet)  as run ${sum(x[3] for x in v):8.2f}  held by day {days}")
