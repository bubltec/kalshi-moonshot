"""Does the side's OWN PRICE still climbing at the buy mark the weak buys, which the spot's swings did not? (2026-11-27; user, on the XRP 06:30 YES re-buy bought 16 s after the lane sold the same side at the top of its climb, the spot's peak reading saying "off the peak": "The peak reading looks at the spot's swings ... this is a bug we need to fix"; "I'd rather have that than a wait. The wait is a patch fix. This is a real fix".)
THE READING (wavefacts.price_swings), no length in it: the side's price (the mid, or 1 - mid for NO) from the market's open to the buy on a 10 s grid is cut into swings by size; BIGGEST is the largest reversal at which it still shows two swings; the swings are read at a quarter and an eighth of it. RISING = the current swing runs up at both sizes (nothing given back at the small scale); otherwise the price has pulled back at one of them.
QUESTION (one, on the lane's own bets): every live PriskP bet since 2026-11-25 15:57 ET with tape, by RISING or pulled back at the buy, by the market's regime (curve or steady, from the buy's own note), and for the bets bought within five minutes after the lane's own sale of the same side: n, how often the market settled the bet's way, held to settlement, as run (the sale after its fee, or settlement, less the price paid; the buys rested).
HONEST LIMITS: the sizes (a quarter and an eighth) were the spot reading's; the split by regime was added after the first table, said so; 312 bets in a day and a half; no asset key. It separates the curve buys strongly and the re-buys weakly: of the eight re-buys that lost the stake, four were still rising at the buy and four had already turned down.
Run: PYTHONPATH=src .venv/bin/python scripts/priskp_price_swings_table.py BETS.json.gz REGIME.txt   (BETS from priskp_exit_tape_pull.py; REGIME.txt lines: ticker|round(ts)|regime)"""
import collections
import gzip
import json
import math
import sys

from kalshi_moonshot import wavefacts

fee = lambda p: 0.07 * p * (1.0 - p)  # noqa: E731
reg = {}
for line in open(sys.argv[2]):
    p = line.strip().split("|")
    if len(p) == 3 and p[0].startswith("KX"):
        reg[(p[0], int(p[1]))] = p[2]
bets = [b for b in json.load(gzip.open(sys.argv[1])) if len(b["bid"]) == 900 and b["result"] in ("yes", "no")]
byk = collections.defaultdict(list)
for b in bets:
    byk[(b["ticker"], b["side"])].append(b)
rows = []
for b in bets:
    side, q0 = b["side"], b["close"] - 900
    k = int(math.ceil(b["ts"])) - q0
    quotes = [(q0 + 1 + i, ((x + y) / 2.0)) for i, (x, y) in enumerate(zip(b["bid"], b["ask"], strict=True)) if x is not None and y is not None and i < k]
    got = wavefacts.price_swings(quotes, float(q0), float(q0 + k), side)
    if got is None:
        continue
    won = b["result"] == side
    prior = [p for p in byk[(b["ticker"], side)] if p["exit_ts"] and 0 < b["ts"] - p["exit_ts"] < 300]
    rows.append({"rising": got["rising"], "won": won, "reg": reg.get((b["ticker"], round(b["ts"])), "?"), "rebuy": bool(prior), "held": ((1.0 if won else 0.0) - b["entry"]) * b["n"], "run": (((b["exit_price"] - fee(b["exit_price"])) if b["exit_ts"] else (1.0 if won else 0.0)) - b["entry"]) * b["n"]})


def line(name: str, v: list) -> None:
    if v:
        print(f"   {name:40s} n {len(v):3d}  settled its way {sum(x['won'] for x in v) / len(v):4.0%}  held ${sum(x['held'] for x in v):8.2f} ({sum(x['held'] for x in v) / len(v):+.2f} a bet)  as run ${sum(x['run'] for x in v):8.2f}")


print(f"{len(rows)} bets with a price reading at the buy")
for title, sel in (("ALL", lambda r: True), ("CURVE markets", lambda r: r["reg"] == "curve"), ("STEADY markets", lambda r: r["reg"] == "steady"), ("RE-BUYS (the lane sold this side within 5 minutes before)", lambda r: r["rebuy"])):
    print("\n" + title)
    line("price rising at both small sizes", [r for r in rows if sel(r) and r["rising"]])
    line("price pulled back at one of them", [r for r in rows if sel(r) and not r["rising"]])
