"""In a curve, was the spot's cushion over the target bigger than the swings the market was making when the lane bought? (2026-11-27; user, on ETH 00:45 YES bought at the top of a bounce 1.6 typical minutes over the target after a first-minute spike had faded back to it, and on the six NO bets of 00:17: "I want it to not make bad buys"; "Just seems like a bad idea with low cushion to buy in a curve like that. Would have been better to wait and it would have picked a different place to bet".)
THE READING, in the market's own units and with no length: at the buy, the spot's distance past the target on the bet's side (its cushion), in the spot's typical minutes of the hour before the open, against two sizes the lane's own curve reading already gives (wavefacts.curve, walked from an hour before the open as the lane now reads it): the swing the bet is riding (swing_u) and the turn it started from, the swing before it the other way (prev_u). HELD BY THE CURVE means the cushion is at least the turn: one more swing against the bet like the last one would not carry the spot across the target.
QUESTION (one, on the lane's own bets): every live PriskP bet since 2026-11-25 15:57 ET with tape that the curve reading calls a curve buy, by the cushion as a share of the turn (under a half, a half to one, one and over): n, how often the market settled the bet's way, the price paid, held to settlement, as run, by day.
HONEST LIMITS: the bins were stated before the run; the idea came from the bad buys of one night; it can only show buys a wait would have held back, not where the lane would have bought instead; a day and a half; no asset key. A lead for the curve entry's existing wait (priskp HELD), scored forward.
Run: PYTHONPATH=src .venv/bin/python scripts/priskp_cushion_vs_swing_table.py BETS.json.gz INDEX.json.gz"""
import datetime
import gzip
import json
import re
import statistics
import sys

from kalshi_moonshot import ticks, wavefacts, waveturn
from kalshi_moonshot.kalshiws import index_for_series

ET = datetime.timezone(datetime.timedelta(hours=-5))
ticks.history.keep = ticks.history.slack = 1e12
for ix, (T, V) in json.load(gzip.open(sys.argv[2])).items():
    for t, v in zip(T, V, strict=True):
        ticks.history.note(ix, float(t), float(v))
fee = lambda p: 0.07 * p * (1.0 - p)  # noqa: E731
rows = []
for b in json.load(gzip.open(sys.argv[1])):
    if len(b["bid"]) != 900 or not b["strike"] or b["result"] not in ("yes", "no"):
        continue
    ix = index_for_series(re.match(r"(KX[A-Z0-9]+15M)", b["ticker"]).group(1))
    opened, left = float(b["close"] - 900), float(b["close"] - b["ts"])
    cv = wavefacts.curve(ix, opened, float(b["ts"]), b["strike"], b["side"], left, wavefacts.HOUR)
    u = waveturn._hour(ix, round(opened)).u
    if cv is None or not u or cv["regime"] != "curve" or cv["verdict"] != "buy" or cv["prev_u"] <= 0:
        continue
    cushion = (cv["spot"] - b["strike"]) * (1.0 if b["side"] == "yes" else -1.0) / u
    won = b["result"] == b["side"]
    rows.append({"ratio": cushion / cv["prev_u"], "cushion": cushion, "turn": cv["prev_u"], "won": won, "paid": b["entry"], "held": ((1.0 if won else 0.0) - b["entry"]) * b["n"], "run": (((b["exit_price"] - fee(b["exit_price"])) if b["exit_ts"] else (1.0 if won else 0.0)) - b["entry"]) * b["n"], "day": datetime.datetime.fromtimestamp(b["ts"], ET).strftime("%m-%d")})


def line(name: str, v: list) -> str:
    days = {d: round(sum(x["held"] for x in v if x["day"] == d), 2) for d in sorted({x["day"] for x in v})}
    return f"   {name:46s} n {len(v):3d}  settled its way {sum(x['won'] for x in v) / len(v):4.0%}  avg paid {statistics.mean(x['paid'] for x in v):.2f}  median cushion {statistics.median(x['cushion'] for x in v):.1f} u, turn {statistics.median(x['turn'] for x in v):.1f} u  held ${sum(x['held'] for x in v):8.2f} ({sum(x['held'] for x in v) / len(v):+.2f} a bet)  as run ${sum(x['run'] for x in v):8.2f}  by day {days}"


print(f"{len(rows)} curve buys (as the lane now reads the curve)\n\nthe spot's cushion over the target at the buy, against the turn the bet's swing started from:")
for name, f in (("cushion under half the turn", lambda r: r < 0.5), ("half the turn to the whole of it", lambda r: 0.5 <= r < 1.0), ("the whole turn and more (held by the curve)", lambda r: r >= 1.0)):
    v = [x for x in rows if f(x["ratio"])]
    if v:
        print(line(name, v))
