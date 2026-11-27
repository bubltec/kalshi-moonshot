"""Was the bet bought WITH or AGAINST the market's swing, at each swing size? (2026-11-26; follows scripts/priskp_swing_scale_study.py, where at the 10 minute travel size the losing bets' first AGAINST reading came a median of 0 s after the buy: most were bought against the big swing.)
QUESTION (one, on the lane's own bets): every live PriskP bet since 2026-11-25 15:57 ET with tape, split by the direction of the spot's zig-zag at the moment of the buy relative to the bet's side (WITH, AGAINST, or no swing of that size yet), for each reversal size of the scale study: n, how often the market settled the bet's way, the price paid, the result held to settlement and as run. The buys rested (no buy fee).
HONEST LIMITS: looked at after the scale study's result; the sizes were stated before that study; a day and a half; no asset key. A lead for one stated entry rule scored forward, not a rule.
Run: PYTHONPATH=src .venv/bin/python scripts/priskp_swing_at_buy_table.py BETS.json.gz INDEX.json.gz"""
import importlib.util
import math
import pathlib
import re
import statistics
import sys

spec = importlib.util.spec_from_file_location("scale", pathlib.Path(__file__).with_name("priskp_swing_scale_study.py"))
src = pathlib.Path(__file__).with_name("priskp_swing_scale_study.py").read_text().split("NAMES = [")[0]  # the readers only: the tape, path(), directions(), sizes()
ns: dict = {"__name__": "scale_readers"}
exec(compile(src, "scale_readers", "exec"), ns)
bets, path, directions, sizes, fee, index_for_series = ns["bets"], ns["path"], ns["directions"], ns["sizes"], ns["fee"], ns["index_for_series"]
NAMES = ["0.5 u (entry's)", "1 u", "2 u", "w3 (3 min travel)", "w10 (10 min travel)", "tau travel"]
rows = {n: {"WITH": [], "AGAINST": [], "none yet": []} for n in NAMES}
for b in bets:
    close, side, entry, strike = b["close"], b["side"], b["entry"], b["strike"]
    if len(b["bid"]) != 900 or not strike or b["result"] not in ("yes", "no"):
        continue
    ix = index_for_series(re.match(r"(KX[A-Z0-9]+15M)", b["ticker"]).group(1))
    opened = close - 900
    xs = path(ix, opened - 1800, close)
    sz = sizes(ix, opened)
    if xs is None or not sz["1 u"]:
        continue
    want = 1 if side == "yes" else -1
    won = b["result"] == side
    k = 1800 + int(math.ceil(b["ts"])) - opened
    run = ((b["exit_price"] - fee(b["exit_price"])) if b["exit_ts"] else (1.0 if won else 0.0)) - entry
    for name in NAMES:
        if not sz[name]:
            continue
        d = directions(xs[: k + 1], sz[name])[-1] * want
        rows[name]["WITH" if d > 0 else "AGAINST" if d < 0 else "none yet"].append((won, entry, ((1.0 if won else 0.0) - entry) * b["n"], run * b["n"]))
for name in NAMES:
    print(name)
    for k in ("WITH", "AGAINST", "none yet"):
        v = rows[name][k]
        if v:
            print(f"   bought {k:9s} n {len(v):3d}  settled its way {sum(x[0] for x in v) / len(v):4.0%}  avg paid {statistics.mean(x[1] for x in v):.2f}  held ${sum(x[2] for x in v):8.2f} ({sum(x[2] for x in v) / len(v):+.2f} a bet)  as run ${sum(x[3] for x in v):8.2f}")

# The two sizes that disagree, together (added after seeing the table above: bought WITH the 10 minute swing did well, bought AGAINST the 3 minute swing did well; is the good bet the pullback inside the big swing?). Chosen after the result, said so.
cross: dict = {}
for b in bets:
    close, side, entry, strike = b["close"], b["side"], b["entry"], b["strike"]
    if len(b["bid"]) != 900 or not strike or b["result"] not in ("yes", "no"):
        continue
    ix = index_for_series(re.match(r"(KX[A-Z0-9]+15M)", b["ticker"]).group(1))
    opened = close - 900
    xs = path(ix, opened - 1800, close)
    sz = sizes(ix, opened)
    if xs is None or not sz["w10 (10 min travel)"] or not sz["w3 (3 min travel)"]:
        continue
    want = 1 if side == "yes" else -1
    won = b["result"] == side
    k = 1800 + int(math.ceil(b["ts"])) - opened
    big = directions(xs[: k + 1], sz["w10 (10 min travel)"])[-1] * want
    small = directions(xs[: k + 1], sz["w3 (3 min travel)"])[-1] * want
    run = ((b["exit_price"] - fee(b["exit_price"])) if b["exit_ts"] else (1.0 if won else 0.0)) - entry
    day = __import__("datetime").datetime.fromtimestamp(b["ts"], __import__("datetime").timezone(__import__("datetime").timedelta(hours=-5))).strftime("%m-%d")
    cross.setdefault(("WITH" if big > 0 else "AGAINST" if big < 0 else "none", "WITH" if small > 0 else "AGAINST" if small < 0 else "none"), []).append((won, entry, ((1.0 if won else 0.0) - entry) * b["n"], run * b["n"], day))
print("\n10 minute swing x 3 minute swing at the buy:")
for key in sorted(cross):
    v = cross[key]
    days = {d: round(sum(x[2] for x in v if x[4] == d), 2) for d in sorted({x[4] for x in v})}
    print(f"   10 min {key[0]:8s} 3 min {key[1]:8s} n {len(v):3d}  settled its way {sum(x[0] for x in v) / len(v):4.0%}  avg paid {statistics.mean(x[1] for x in v):.2f}  held ${sum(x[2] for x in v):8.2f} ({sum(x[2] for x in v) / len(v):+.2f} a bet)  as run ${sum(x[3] for x in v):8.2f}  held by day {days}")
