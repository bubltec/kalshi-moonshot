"""The curve table: what has become of a 15-minute crypto move that looked like this one.

At the start of minute m of a window, the minute-by-minute closes of the settlement index since the window opened (m of them) put the move in one of 36 classes:
  ahead or behind the opening price so far  x  which third of the window's range the latest close is in  x  how long ago the window's high was (now, 1-2 minutes, 3 or more)  x  whether the last 3 minutes rose or fell.
The baseline chance of finishing above the open is the random-walk one: Phi(z), z = the move so far in units of what the coin could still move (its per-minute volatility over the previous 240 minutes x sqrt(minutes left)).
The table holds, per coin and per minute, a shift in log-odds for each class: how much more (or less) often moves of that class finished up than the baseline said, learned from the most recent months of 1-minute data
(scripts/curve_table_fit.py writes data/curve_table.json). A thin class is pulled toward zero. With no table, or a class the table has no entry for, the chance is just the baseline.

Years of per-coin history show the classes carry information beyond the baseline (see scripts/shape_class_study.py); whether Kalshi's own prices already contain it is what the paper lane `curve_paper` is there to find out. Nothing here places an order.
"""

from __future__ import annotations

import itertools
import json
import math
import os
from typing import Any

MIN_MINUTE, MAX_MINUTE = 5, 12  # the minutes of the window the table covers (it needs at least 4 closes for the last-3-minutes direction)
TABLE_FILE = "curve_table.json"


def phi(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def per_minute_vol(closes: list[float]) -> float | None:
    """Standard deviation of the one-minute log returns of `closes` (oldest first); None with fewer than 30 closes."""
    if len(closes) < 30:
        return None
    rets = [math.log(b / a) for a, b in itertools.pairwise(closes) if a > 0 and b > 0]
    if len(rets) < 29:
        return None
    mean = sum(rets) / len(rets)
    return math.sqrt(max(sum((r - mean) ** 2 for r in rets) / len(rets), 1e-14))


def classify(open_value: float, path: list[float], vol_1m: float) -> dict[str, Any] | None:
    """The class and the baseline chance of finishing above the open, from the closes of the first m minutes (`path`, oldest first). None when m is outside the table's minutes or the numbers are unusable."""
    m = len(path)
    if not (MIN_MINUTE <= m <= MAX_MINUTE) or open_value <= 0 or vol_1m <= 0 or min(path) <= 0:
        return None
    so_far = math.log(path[-1] / open_value)
    base = min(1 - 1e-4, max(1e-4, phi(so_far / (vol_1m * math.sqrt(15 - m)))))
    hi, lo = max(path), min(path)
    pos = (path[-1] - lo) / (hi - lo) if hi > lo else 0.0
    since_high = m - 1 - path.index(hi)
    pace = math.log(path[-1] / path[-4])
    ahead, third = int(so_far > 0), min(int(pos * 3), 2)
    bucket = 0 if since_high == 0 else 1 if since_high <= 2 else 2
    rising = int(pace > 0)
    return {"class": ahead * 18 + third * 6 + bucket * 2 + rising, "minute": m, "baseline": base, "so_far": so_far, "range_pos": pos, "since_high": since_high, "pace": pace}


def describe(cls: int) -> str:
    ahead, rest = divmod(cls, 18)
    third, rest = divmod(rest, 6)
    bucket, rising = divmod(rest, 2)
    return f"{'ahead' if ahead else 'behind'}, {['low', 'middle', 'top'][third]} third of its range, high {['now', '1-2 min ago', '3+ min ago'][bucket]}, last 3 min {'rising' if rising else 'falling'}"


class CurveTable:
    def __init__(self, raw: dict[str, Any]):
        self.coins: dict[str, dict[str, dict[str, dict[str, float]]]] = raw.get("coins", {})
        self.months = raw.get("months")
        self.made_from = raw.get("made_from", "")

    def shift(self, series: str, minute: int, cls: int) -> tuple[float, int]:
        """(the log-odds shift, the training windows behind it) for this class; (0, 0) when the table has none."""
        entry = self.coins.get(series, {}).get(str(minute), {}).get(str(cls))
        return (float(entry["delta"]), int(entry["n"])) if entry else (0.0, 0)

    def chance_up(self, series: str, info: dict[str, Any]) -> tuple[float, float, int]:
        """(the table's chance of finishing above the open, the baseline chance, the windows behind the class)."""
        shift, n = self.shift(series, info["minute"], info["class"])
        base = info["baseline"]
        lg = math.log(base / (1 - base)) + shift
        return min(1 - 1e-4, max(1e-4, 1 / (1 + math.exp(-lg)))), base, n


_cache: dict[str, tuple[float, CurveTable]] = {}


def load_table(data_dir: str | os.PathLike[str]) -> CurveTable | None:
    path = os.path.join(data_dir, TABLE_FILE)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    hit = _cache.get(path)
    if hit and hit[0] == mtime:
        return hit[1]
    with open(path) as f:
        table = CurveTable(json.load(f))
    _cache[path] = (mtime, table)
    return table


def minute_closes(tape: Any, index_id: str, open_ts: float, now: float, back_minutes: int = 240) -> tuple[list[float], list[float], float | None]:
    """(closes of the window's whole minutes so far, closes of the `back_minutes` minutes before the window opened, the index value at the open), each from the last index tick at or before the minute's end.
    The index ticks come about once a second, so a tick more than 30 s old is not a reading and the answer is empty."""
    first = open_ts - back_minutes * 60 - 60
    rows = tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND ts>? AND ts<=? AND value > 0 ORDER BY ts", (index_id, first, now)).fetchall()
    if not rows:
        return [], [], None
    import bisect

    ts = [r[0] for r in rows]

    def at(t: float) -> float | None:
        i = bisect.bisect_right(ts, t) - 1
        return float(rows[i][1]) if i >= 0 and t - ts[i] < 30.0 else None

    open_value = at(open_ts)
    whole = int((now - open_ts) // 60)
    path = [v for k in range(1, whole + 1) if (v := at(open_ts + 60.0 * k)) is not None]
    before = [v for k in range(-back_minutes, 1) if (v := at(open_ts + 60.0 * k)) is not None]
    return (path if len(path) == whole else []), before, open_value
