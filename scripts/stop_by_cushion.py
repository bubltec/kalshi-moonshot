"""The lane's own max-loss sales, by how far the spot was across the target when it sold (2026-11-16, user, on DOGE 18:15 NO sold at 0.36 as the coin crossed the target: "it barely cross over the target and sold"; "it needs to wait longer when the cushion is still low (even when losing)").

One question on the same bets (CLAUDE.md rule 4): every live crypto_multiscale max-loss sale since 2026-11-14 17:02 ET, against holding that bet to settlement, fee on every leg. Split by the bet side's cushion at the second of the sale: the settlement index against the market's strike, in units of what the index typically moves in the time left (scenario.cushion, the typical move read from the hour before the market's open). The index is read from the tape's ticks stamped at or before the sale; the typical move from ticks before the open. Negative = the spot is across the target against the bet. Bands fixed before the run. No coin is named.

Run on the host: scripts/cloud.sh run scripts/stop_by_cushion.py
"""
import datetime

import bisect
import math
import types
from typing import Sequence

from kalshi_moonshot import config, db, variants
from kalshi_moonshot.strategy import fee_per_contract as fee

MINUTE = 60.0


# scenario.scope and scenario.cushion, copied here so the script also runs on an image that predates scenario.py
def _minute_marks(ts: Sequence[float], vs: Sequence[float], end: float, minutes: int) -> list[float] | None:
    """The index at each minute mark from `minutes` ago to `end` (the last tick at or before each), oldest first. None when a mark has no tick within 90 s before it."""
    out = []
    for k in range(minutes, -1, -1):
        at = end - k * MINUTE
        i = bisect.bisect_right(ts, at) - 1
        if i < 0 or at - ts[i] > 90.0 or vs[i] <= 0:
            return None
        out.append(vs[i])
    return out


def scope(ts: Sequence[float], vs: Sequence[float], end: float, minutes: int, ref: float) -> dict[str, float] | None:
    """One scope ending at `end`: `place` of `ref` in the index's range (0 = at the low, 1 = at the high; outside 0..1 when `ref` is beyond the range), `drift` = the index's travel across the scope in its own typical one-minute moves x sqrt(minutes) (+ = up), `unit` = that typical one-minute move as a fraction of the price. None without a tick near every minute mark or with a flat scope."""
    marks = _minute_marks(ts, vs, end, minutes)
    if marks is None or ref <= 0:
        return None
    a = bisect.bisect_right(ts, end - minutes * MINUTE)
    b = bisect.bisect_right(ts, end)
    lo, hi = min(vs[a:b], default=0.0), max(vs[a:b], default=0.0)
    moves = [math.log(marks[k + 1] / marks[k]) for k in range(len(marks) - 1)]
    unit = math.sqrt(sum(m * m for m in moves) / len(moves))
    if hi <= lo or unit <= 0:
        return None
    return {"place": (ref - lo) / (hi - lo), "drift": math.log(marks[-1] / marks[0]) / (unit * math.sqrt(minutes)), "unit": unit}


def cushion(ts: Sequence[float], vs: Sequence[float], now: float, tgt: float, side: str, left: float, unit: float) -> float | None:
    """How far the spot sits on the side's side of the target, in units of what the index typically moves in the time left (`unit` per sqrt-minute, from a scope). None with a stale spot (over 20 s), no unit or no time left."""
    i = bisect.bisect_right(ts, now) - 1
    if i < 0 or now - ts[i] > 20.0 or vs[i] <= 0 or tgt <= 0 or unit <= 0 or left <= 0:
        return None
    return (1.0 if side == "yes" else -1.0) * math.log(vs[i] / tgt) / (unit * math.sqrt(left / MINUTE))


scenario = types.SimpleNamespace(scope=scope, cushion=cushion)
ET = datetime.timezone(datetime.timedelta(hours=-5))
SINCE = datetime.datetime(2026, 11, 14, 17, 2, tzinfo=ET).timestamp()
s = config.load()
st = db.connect(s.db_path, "main", shared=True)
tape = db.connect(s.tape_path, "tape", shared=True)
win = {w["ticker"]: (w["result"], float(w["close_ts"]), float(w["strike"] or 0.0)) for w in st.execute("SELECT ticker, close_ts, result, strike FROM windows WHERE close_ts>? AND result IN ('yes','no')", (SINCE,)).fetchall()}
rows, unread = [], 0
for d in st.execute("SELECT ts, ticker, side, ask, stake_usd, exit_price, exit_ts FROM decisions WHERE lane='crypto_multiscale' AND mode='trade' AND action='bet' AND stake_usd>0 AND ts>? AND exit_reason LIKE '%(max loss)%' ORDER BY ts", (SINCE,)).fetchall():
    if d["ticker"] not in win or d["exit_price"] is None or not d["ask"]:
        continue
    result, close, strike = win[d["ticker"]]
    at, opened = float(d["exit_ts"]), close - 900.0
    ix = variants.crypto_index(d["ticker"].split("-")[0])
    tk = tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND ts BETWEEN ? AND ? AND value>0 ORDER BY ts", (ix, opened - 3700.0, at)).fetchall() if ix else []
    ts, vs = [float(x[0]) for x in tk], [float(x[1]) for x in tk]
    sc = scenario.scope(ts, vs, opened, 60, strike) if strike > 0 else None
    cush = scenario.cushion(ts, vs, at, strike, d["side"], close - at, sc["unit"]) if sc else None
    a, px = float(d["ask"]), float(d["exit_price"])
    basis = a + fee(a)
    n = float(d["stake_usd"]) / basis
    won = result == d["side"]
    sale, hold = (px - fee(px) - basis) * n, ((1.0 if won else 0.0) - basis) * n
    if cush is None:
        unread += 1
    rows.append((cush, sale - hold, won, close - at, px))


def line(name, rs):
    if len(rs) < 4:
        print(f"  {name:44s} n {len(rs):3d}")
        return
    n = len(rs)
    print(f"  {name:44s} n {n:3d} | would have won held {sum(1 for r in rs if r[2]):3d} ({sum(1 for r in rs if r[2]) / n:4.0%}) | sold at {sum(r[4] for r in rs) / n:.2f} | sale less hold ${sum(r[1] for r in rs):+8.2f} (saved ${sum(r[1] for r in rs if r[1] > 0):+.2f} on {sum(1 for r in rs if r[1] > 0)}, gave up ${sum(r[1] for r in rs if r[1] < 0):+.2f} on {sum(1 for r in rs if r[1] < 0)})")


print(f"window: since 2026-11-14 17:02 ET; {len(rows)} max-loss sales, {unread} without a cushion reading (no index or no strike)")
line("all", rows)
read = [r for r in rows if r[0] is not None]
print("\n1. by the bet side's cushion at the sale (typical moves for the time left; negative = across the target)")
for lo, hi, name in ((0.0, 99, "still on the bet's side (0 and over)"), (-0.25, 0.0, "barely across (0 to -0.25)"), (-0.5, -0.25, "-0.25 to -0.5"), (-1.0, -0.5, "-0.5 to -1"), (-99, -1.0, "clearly across (under -1)")):
    line(name, [r for r in read if lo <= r[0] < hi])
print("\n2. the same, with over 3 minutes left and with 3 or less")
for tname, pick in (("over 3 min left", lambda r: r[3] > 180), ("3 min or less left", lambda r: r[3] <= 180)):
    for lo, hi, name in ((-0.5, 99, "cushion over -0.5 (on the line)"), (-99, -0.5, "cushion under -0.5 (across)")):
        line(f"{tname}, {name}", [r for r in read if pick(r) and lo <= r[0] < hi])
print("DONE")
