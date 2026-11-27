"""How do the market's own swings NEST at the buy, and which nests are the good bets? (2026-11-26; user: "Don't build on waves that are specific lengths"; "It needs to handle the curves like it has been getting better at and keep inlroving". Follows scripts/priskp_own_swings_at_buy_table.py, where one swing size at a time did not separate the bets, and the traces of the bets of 22:30 to 23:00 that night, where every loser was a small swing bought against the larger move the market had opened inside, a move the lane's leg reading cannot see because it walks back only to the open.)
THE READING, no length in it: the spot the lane could see at the buy (the hour before the open and the market so far) is cut into swings by size. BIGGEST is the largest reversal at which the path still shows a turn; the levels are 1, 1/2, 1/4 and 1/8 of it. At each level the bet's side runs WITH (+) or AGAINST (-) the current swing. The NEST is the four signs from the biggest level down, e.g. "+ + - +": with the biggest two, against the quarter, with the eighth.
QUESTION (one, on the lane's own bets): every live PriskP bet since 2026-11-25 15:57 ET with tape, by its nest at the buy and by two summaries of it (how many of the four levels run with the bet; whether the larger half of the nest and the smaller half agree): n, how often the market settled the bet's way, the price paid, held to settlement, as run, by day.
HONEST LIMITS: the levels were stated before the run; the summaries were chosen from the traces of one evening, before this table was run on all the bets; sixteen nests on 210 bets are thin, so read the summaries; a day and a half; no asset key. A lead for the curve reading, not a rule.
Run: PYTHONPATH=src .venv/bin/python scripts/priskp_nested_swings_at_buy_table.py BETS.json.gz INDEX.json.gz"""
import collections
import datetime
import math
import pathlib
import re
import statistics
import sys

src = pathlib.Path(__file__).with_name("priskp_own_swings_at_buy_table.py").read_text().split("rows = {s:")[0]  # the readers: the tape, path(), swings(), biggest()
ns: dict = {"__name__": "own_readers", "__file__": str(pathlib.Path(__file__).with_name("priskp_own_swings_at_buy_table.py"))}
exec(compile(src, "own_readers", "exec"), ns)
bets, path, fee, index_for_series, swings, biggest = ns["bets"], ns["path"], ns["fee"], ns["index_for_series"], ns["swings"], ns["biggest"]
ET = datetime.timezone(datetime.timedelta(hours=-5))
LEVELS = (1.0, 0.5, 0.25, 0.125)
nests, count, halves = collections.defaultdict(list), collections.defaultdict(list), collections.defaultdict(list)
for b in bets:
    close, side, entry, strike = b["close"], b["side"], b["entry"], b["strike"]
    if len(b["bid"]) != 900 or not strike or b["result"] not in ("yes", "no"):
        continue
    ix = index_for_series(re.match(r"(KX[A-Z0-9]+15M)", b["ticker"]).group(1))
    xs = path(ix, close - 900 - 3600, int(math.ceil(b["ts"])))
    if xs is None or len(xs) < 600:
        continue
    R = biggest(xs)
    if R <= 0:
        continue
    want = 1 if side == "yes" else -1
    sig = [swings(xs, R * s * 0.999)[1] * want for s in LEVELS]
    if 0 in sig:
        continue
    won = b["result"] == side
    run = ((b["exit_price"] - fee(b["exit_price"])) if b["exit_ts"] else (1.0 if won else 0.0)) - entry
    row = (won, entry, ((1.0 if won else 0.0) - entry) * b["n"], run * b["n"], datetime.datetime.fromtimestamp(b["ts"], ET).strftime("%m-%d"))
    nests[" ".join("+" if v > 0 else "-" for v in sig)].append(row)
    count[sum(v > 0 for v in sig)].append(row)
    big, small = sig[0] + sig[1], sig[2] + sig[3]
    name = {(1, 1): "larger WITH, smaller WITH", (1, -1): "larger WITH, smaller AGAINST (a pullback inside the move)", (-1, 1): "larger AGAINST, smaller WITH (a bounce against the move)", (-1, -1): "larger AGAINST, smaller AGAINST"}.get((int(math.copysign(1, big)) if big else 0, int(math.copysign(1, small)) if small else 0), "mixed (a level split in a half)")
    halves[name].append(row)


def line(name: str, v: list) -> str:
    days = {d: round(sum(x[2] for x in v if x[4] == d), 2) for d in sorted({x[4] for x in v})}
    return f"   {name:58s} n {len(v):3d}  settled its way {sum(x[0] for x in v) / len(v):4.0%}  avg paid {statistics.mean(x[1] for x in v):.2f}  held ${sum(x[2] for x in v):8.2f} ({sum(x[2] for x in v) / len(v):+.2f} a bet)  as run ${sum(x[3] for x in v):8.2f}  by day {days}"


print("how many of the four levels run WITH the bet:")
for k in sorted(count):
    print(line(f"{k} of 4", count[k]))
print("\nthe larger half of the nest (biggest, half) against the smaller half (quarter, eighth):")
for k in sorted(halves):
    print(line(k, halves[k]))
print("\nevery nest (biggest, 1/2, 1/4, 1/8):")
for k in sorted(nests, key=lambda k: -len(nests[k])):
    print(line(k, nests[k]))
