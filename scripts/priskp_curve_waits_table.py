"""Which of three waits on the curve entry hold back the weak buys? (2026-11-27; user, on re-buys and a buy "at the end peak of a U shaped curve (with almost no cushion)": "I have never asked for that logic"; "why doesn't the rebuy follow priskp logic, which should have done none of this and waited for the next curve"; "I'd rather have that than a wait. The wait is a patch fix. This is a real fix".)
THE THREE WAITS, each a reading of the market's own curve, none a timer: (1) PRICE RISING: the side's own price is still climbing at both small swing sizes (wavefacts.price_swings: a quarter and an eighth of the market's biggest price swing); (2) LOW CUSHION: the spot's cushion over the target is under half the turn the bet's swing started from, both in the market's typical minutes (priskp.LOW_CUSHION_SHARE; bins stated before the run: under half, half to one, one and over); (3) PLAYED: the lane already bought the swing the side is in (the leg reading's current swing, wavefacts.leg walked from an hour before the open, the same start within its 30 s smoothing).
QUESTION (one, on the lane's own bets): of the curve buys the lane makes (wavefacts.curve regime curve, verdict buy, read from an hour before the open) on every live PriskP bet since 2026-11-25 15:57 ET with tape, which would each wait have held back, and what did the held-back and the left buys make: n, how often the market settled the bet's way, held to settlement, as run (the sale after its fee, or settlement, less the price paid; the buys rested).
HONEST LIMITS: the half-turn bin edge is the lower bin of three stated before the run; the price reading's sizes are the spot reading's; the played test needs the earlier bet's own reading; 167 curve buys in a day and a half, so the three together leave 21 (about one in eight); the waits can only show buys they hold back, not where the lane then buys instead; no asset key. A lead, scored forward.
Inputs: BETS.json.gz (scripts/priskp_exit_tape_pull.py), the spot index ticks loaded into ticks.history (scripts/priskp_index_tape_pull.py, or the bulk COPY per index the overnight watch used).
Run: PYTHONPATH=src .venv/bin/python scripts/priskp_curve_waits_table.py BETS.json.gz INDEX_TAPE.json.gz [EXTRA_INDEX_DIR_OF_CSVS]"""
import collections
import gzip
import json
import math
import os
import re
import sys

from kalshi_moonshot import ticks, wavefacts, waveturn
from kalshi_moonshot.kalshiws import index_for_series

ticks.history.keep = ticks.history.slack = 1e12
for ix, (T, V) in json.load(gzip.open(sys.argv[2])).items():
    for t, v in zip(T, V, strict=True):
        ticks.history.note(ix, float(t), float(v))
if len(sys.argv) > 3:  # later ticks, one CSV (second,value) per index named <index>.csv
    for f in sorted(os.listdir(sys.argv[3])):
        last = ticks.history.last_at_or_before(f[:-4], 1e12)
        lt = last[0] if last else 0.0
        for line in open(os.path.join(sys.argv[3], f)):
            p = line.strip().split(",")
            if len(p) == 2 and p[0].isdigit() and float(p[0]) > lt:
                ticks.history.note(f[:-4], float(p[0]), float(p[1]))
fee = lambda p: 0.07 * p * (1.0 - p)  # noqa: E731
bets = [b for b in json.load(gzip.open(sys.argv[1])) if len(b["bid"]) == 900 and b["result"] in ("yes", "no") and b["strike"]]
byk = collections.defaultdict(list)
for b in bets:
    byk[(b["ticker"], b["side"])].append(b)
for lst in byk.values():
    lst.sort(key=lambda b: b["ts"])
rows = []
for b in bets:
    ix = index_for_series(re.match(r"(KX[A-Z0-9]+15M)", b["ticker"]).group(1))
    opened = float(b["close"] - 900)
    cv = wavefacts.curve(ix, opened, float(b["ts"]), b["strike"], b["side"], float(b["close"] - b["ts"]), wavefacts.HOUR)
    if cv is None or cv["regime"] != "curve" or cv["verdict"] != "buy":
        continue
    side, q0 = b["side"], b["close"] - 900
    k = int(math.ceil(b["ts"])) - q0
    quotes = [(q0 + 1 + i, (x + y) / 2.0) for i, (x, y) in enumerate(zip(b["bid"], b["ask"], strict=True)) if x is not None and y is not None and i < k]
    pr = wavefacts.price_swings(quotes, float(q0), float(q0 + k), side)
    played = False
    for p in byk[(b["ticker"], side)]:
        if p["ts"] >= b["ts"]:
            break
        c0 = wavefacts.curve(ix, opened, float(p["ts"]), b["strike"], side, float(b["close"] - p["ts"]), wavefacts.HOUR)
        if c0 and abs(c0["swing_start"] - cv["swing_start"]) <= waveturn.SMOOTH:
            played = True
    won = b["result"] == side
    rows.append({"rising": bool(pr and pr["rising"]), "low": cv["prev_u"] > 0 and cv["cushion_u"] < 0.5 * cv["prev_u"], "played": played, "won": won, "held": ((1.0 if won else 0.0) - b["entry"]) * b["n"], "run": (((b["exit_price"] - fee(b["exit_price"])) if b["exit_ts"] else (1.0 if won else 0.0)) - b["entry"]) * b["n"]})


def line(name: str, v: list) -> None:
    if v:
        print(f"   {name:58s} n {len(v):3d}  settled its way {sum(x['won'] for x in v) / len(v):4.0%}  held ${sum(x['held'] for x in v):8.2f} ({sum(x['held'] for x in v) / len(v):+.2f} a bet)  as run ${sum(x['run'] for x in v):8.2f}")


print(f"{len(rows)} curve buys the lane makes\n")
line("all curve buys", rows)
for name, f in (("price still rising", lambda r: r["rising"]), ("cushion under half the turn", lambda r: r["low"]), ("the same swing as an earlier bet of the lane", lambda r: r["played"])):
    line("HELD BACK by: " + name, [r for r in rows if f(r)])
    line("   not held back by it", [r for r in rows if not f(r)])
anyf = lambda r: r["rising"] or r["low"] or r["played"]  # noqa: E731
print()
line("held back by any of the three", [r for r in rows if anyf(r)])
line("LEFT after all three", [r for r in rows if not anyf(r)])
line("left after price rising and low cushion only", [r for r in rows if not (r["rising"] or r["low"])])
