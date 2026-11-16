"""Does the live lane pick worse moments than the price band alone? (2026-11-16, user: "i often see a YES that should have been a NO a few minutes later ... something with how we choose to buy is off")

One question, same markets (CLAUDE.md rule 4): the live crypto_multiscale buys since 2026-11-14 17:02 ET (the width change; the lane's logic before it is not this lane), each held to settlement at the ask it was decided at, against EVERY moment in the same series' settled markets where a side sat in the lane's own band (ask 0.57 to 0.82, spread 0.03 or under, 3 minutes in or later), read every 15 seconds from Kalshi's own quotes. If the lane's gates and fills chose well, its buys beat the band; if they chose badly, the band beats them. The fee is on every leg.
The shapes are read from the side's own Kalshi quote curve before the moment: where its mid sits in its own range so far (0 = its low, 1 = its high) and its move over the last 3 minutes in the market's own typical minutes. No coin is named.
Then the user's observation itself, on the lane's buys: how soon after the buy the side was no longer the favorite (mid under 0.50), and what the other side paid from the same moment.

Run on the host: scripts/cloud.sh run scripts/entry_selection_study.py
"""
import bisect
import datetime
import math
import io
import os
import re
import sys
from collections import defaultdict

from kalshi_moonshot import config, db
from kalshi_moonshot.strategy import fee_per_contract

ET = datetime.timezone(datetime.timedelta(hours=-5))
SINCE = datetime.datetime(2026, 11, 14, 17, 2, tzinfo=ET).timestamp()
ALWAYS = datetime.datetime(2026, 11, 15, 16, 31, tzinfo=ET).timestamp()
LO, HI, SPREAD, FROM = 0.57, 0.82, 0.03, 180.0
STEP = 15
s = config.load()
st = db.connect(s.db_path, "main", shared=True)
tape = db.connect(s.tape_path, "tape", shared=True)

bets = {}
for r in st.execute("SELECT ts, ticker, side, ask, stake_usd, reason FROM decisions WHERE lane='crypto_multiscale' AND mode='trade' AND action='bet' AND stake_usd>0 AND ts>? ORDER BY ts", (SINCE,)).fetchall():
    ce = re.search(r"\[curve edge ([+-][\d.]+)\]", str(r["reason"] or ""))  # the curve's own chance less the ask, as the buy recorded it
    bets.setdefault((r["ticker"], r["side"]), (float(r["ts"]), float(r["ask"] or 0.0), float(r["stake_usd"]), float(ce.group(1)) if ce else None))
series = sorted({k[0].split("-")[0] for k in bets})
wins = [w for w in st.execute("SELECT ticker, series, close_ts, result FROM windows WHERE close_ts>? AND result IN ('yes','no') ORDER BY close_ts", (SINCE,)).fetchall() if w["series"] in series]
by_ticker = defaultdict(list)
for k, v in bets.items():
    by_ticker[k[0]].append((k[1], v))


def net(ask, won):
    return (1.0 if won else 0.0) / (ask + fee_per_contract(ask)) - 1.0


def grid(ticker, opened, close):
    """Yes bid and ask at each second mark from the open, forward-filled; None before the first two-sided quote."""
    rows = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? ORDER BY ts", (ticker, opened - 20, close)).fetchall()
    rows = [(float(x[0]), float(x[1] or 0.0), float(x[2] or 0.0)) for x in rows if x[2] and float(x[2]) > 0 and not (float(x[1] or 0.0) <= 0 and float(x[2]) >= 1)]
    ts = [x[0] for x in rows]

    def at(t):
        i = bisect.bisect_right(ts, t) - 1
        return (rows[i][1], rows[i][2]) if i >= 0 else None

    return at


def shape(at, opened, t, side):
    """(position of the side's mid in its own range so far, its 3-minute move in typical minutes) or None."""
    mids = []
    for u in range(int(opened), int(t) + 1, STEP):
        q = at(u)
        if q:
            m = (q[0] + q[1]) / 2
            mids.append(m if side == "yes" else 1 - m)
    q = at(t)
    if not q or len(mids) < 13:
        return None
    now = (q[0] + q[1]) / 2 if side == "yes" else 1 - (q[0] + q[1]) / 2
    lo, hi = min(mids + [now]), max(mids + [now])
    moves = [b - a for a, b in zip(mids, mids[1:])]
    unit = math.sqrt(sum(m * m for m in moves) / len(moves)) * math.sqrt(60 / STEP)
    if hi - lo <= 0 or unit <= 0:
        return None
    return (now - lo) / (hi - lo), (now - mids[-13]) / (unit * math.sqrt(3))


def bucket_shape(sh):
    if sh is None:
        return "no reading"
    pos, z = sh
    return ("at its high (top fifth of its range)" if pos >= 0.8 else "mid range" if pos >= 0.4 else "low in its range") + (", rising" if z > 0.5 else ", falling" if z < -0.5 else ", flat")


band = []  # (ask, won, elapsed, shape, ticker)
no_tape = 0
mine = []  # (ask, won, elapsed, shape, ts, stake, flips, other ask, live after always)
for w in wins:
    close = float(w["close_ts"])
    opened = close - 900.0
    at = grid(w["ticker"], opened, close)
    for t in range(int(opened + FROM), int(close) - 15, STEP):
        q = at(t)
        if not q:
            continue
        for side in ("yes", "no"):
            bid, ask = (q[0], q[1]) if side == "yes" else (1 - q[1], 1 - q[0])
            if LO <= ask <= HI and ask - bid <= SPREAD:
                band.append((ask, w["result"] == side, t - opened, bucket_shape(shape(at, opened, t, side)), w["ticker"]))
    for side, (ts_, ask, stake, edge) in by_ticker.get(w["ticker"], ()):
        q = at(ts_)
        if not q:
            no_tape += 1  # the tape has no quote for this market (the recorder followed no market from 2026-11-16 00:31 ET): the buy has no path to read and is left out
            continue
        if not (0.05 < ask < 0.99) and q:
            ask = q[1] if side == "yes" else 1 - q[0]
        if not (0.05 < ask < 0.99):
            continue
        first_under = None
        for u in range(int(ts_) + 5, int(close), 5):
            qq = at(u)
            if qq and ((qq[0] + qq[1]) / 2 if side == "yes" else 1 - (qq[0] + qq[1]) / 2) < 0.5:
                first_under = u - ts_
                break
        other = (1 - q[0] if side == "yes" else q[1]) if q else None
        mine.append((ask, w["result"] == side, ts_ - opened, bucket_shape(shape(at, opened, ts_, side)), ts_, stake, first_under, other, ts_ >= ALWAYS, edge))


def line(name, rows):
    if not rows:
        print(f"  {name:46s} n    0")
        return
    n = len(rows)
    won = sum(1 for r in rows if r[1]) / n
    ask = sum(r[0] for r in rows) / n
    need = sum(r[0] + fee_per_contract(r[0]) for r in rows) / n
    print(f"  {name:46s} n {n:6d} | won {won:4.0%} | ask {ask:.3f} | needs {need:4.0%} | held net per $ {sum(net(r[0], r[1]) for r in rows) / n * 100:+6.1f}%")


print(f"window: since 2026-11-14 17:02 ET; {len(mine)} settled live crypto_multiscale buys (of {len(bets)} placed), {no_tape} more left out with no quotes on the tape, {len(wins)} settled markets, {len({b[4] for b in band})} of them with a moment in the band")
out = sys.stdout
if os.environ.get("TAIL") == "2":
    sys.stdout = io.StringIO()  # sections 1 to 6 are computed and not printed
print("\n1. the lane's buys against every moment in its own band (held to settlement at the ask, fee in)")
line("lane's buys", mine)
line("lane's buys since always = true (11-15 16:31)", [m for m in mine if m[8]])
line("every band moment", band)
TAIL = os.environ.get("TAIL") in ("1", "2")  # the host session drops long printouts: TAIL=1 skips sections 2 and 3, TAIL=2 prints 7 to 9 only
if not TAIL:
  print("\n2. by price")
  for lo, hi in ((0.57, 0.66), (0.66, 0.74), (0.74, 0.83)):
      line(f"buys  ask {lo:.2f} to {hi:.2f}", [m for m in mine if lo <= m[0] < hi])
      line(f"band  ask {lo:.2f} to {hi:.2f}", [b for b in band if lo <= b[0] < hi])
  print("\n3. by minutes into the market")
  for lo, hi in ((3, 6), (6, 9), (9, 15)):
      line(f"buys  {lo} to {hi} min", [m for m in mine if lo * 60 <= m[2] < hi * 60])
      line(f"band  {lo} to {hi} min", [b for b in band if lo * 60 <= b[2] < hi * 60])
print("\n4. by the side's own curve before the moment (position in its range so far; 3-minute move in typical minutes)")
for name in sorted({b[3] for b in band} | {m[3] for m in mine}):
    line(f"buys  {name}", [m for m in mine if m[3] == name])
    line(f"band  {name}", [b for b in band if b[3] == name])
print("\n5. the lane's buys: how soon the side was no longer the favorite (its mid under 0.50)")
n = len(mine)
for lim in (60, 120, 180, 300, 900):
    hit = [m for m in mine if m[6] is not None and m[6] <= lim]
    back = sum(1 for m in hit if m[1])
    print(f"  within {lim:3d} s: {len(hit):3d} of {n} ({len(hit) / max(n, 1):4.0%}); of those {back} still won ({back / max(len(hit), 1):4.0%})")
never = [m for m in mine if m[6] is None]
print(f"  never under 0.50: {len(never)} of {n}; won {sum(1 for m in never if m[1])}")
print("\n6. the other side bought at the same moment, held to settlement (fee in)")
opp = [(m[7], not m[1]) for m in mine if m[7] is not None and 0.03 < m[7] < 0.97]
line("other side at the lane's moments", opp)
opp_band = [(1 - b[0] + 0.01, not b[1]) for b in band]
line("other side at every band moment (ask = 1 - ask + 1c)", opp_band)
sys.stdout = out
print("\n7. the lane's buys by the curve's own reading at the buy (curve edge = the curve's chance less the ask, from the buy's note)")
EDGES = ((-9, -0.03, "edge under -0.03"), (-0.03, 0.0, "edge -0.03 to 0"), (0.0, 0.03, "edge 0 to +0.03"), (0.03, 9, "edge +0.03 and over"))
line("no curve edge in the note", [m for m in mine if m[9] is None])
for lo, hi, name in EDGES:
    line(name, [m for m in mine if m[9] is not None and lo <= m[9] < hi])
print("\n8. the same, early and late (minutes into the market)")
for a, b in ((3, 6), (6, 9), (9, 15)):
    for lo, hi, name in ((-9, 0.0, "edge under 0"), (0.0, 9, "edge 0 and over")):
        line(f"{a} to {b} min, {name}", [m for m in mine if m[9] is not None and lo <= m[9] < hi and a * 60 <= m[2] < b * 60])
print("\n9. is the early weakness the top-of-range weakness? buys by minutes x position")
for a, b in ((3, 6), (6, 9), (9, 15)):
    for name, pick in (("top fifth of its range", lambda m: m[3].startswith("at its high")), ("lower in its range", lambda m: not m[3].startswith("at its high") and m[3] != "no reading")):
        line(f"{a} to {b} min, {name}", [m for m in mine if pick(m) and a * 60 <= m[2] < b * 60])
print("DONE")
