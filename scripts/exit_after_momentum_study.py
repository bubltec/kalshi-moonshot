"""After a momentum entry into the 70-90c band, what does each kind of exit do? Replayed from the tape. Places nothing.

Entry: the first moment, per market and side, that the ask is 70-90c (book within 8c), the trend z (see theta_trend_study.py) is at least 1.5, and the market is active.
Exits, from that moment (fees: the entry fee, plus the sale fee when it sells):
  hold         to the result
  trail        the crash exit as the live lanes run it: arms once the bid has covered 30% of the way to $1.00, sells after 90% of the gain is given back (or a 20c fall at once), 10 s confirm
  stop 30/50   sell when the bid is 30% / 50% under the price paid for 10 s (loss protection only)
  both         trail and a 50% stop
Standard errors are taken across clusters (one 15-minute slot across coins).
    docker compose exec -T bot python - < scripts/exit_after_momentum_study.py
"""
import bisect
import math
import re
import statistics
import time
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

from kalshi_moonshot import config, db
from kalshi_moonshot.kalshi import KalshiClient
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee

s = config.reload()
tape = db.connect(s.tape_path, "tape", shared=True)
main = Store(s.db_path, s.exclude_versions)
client = KalshiClient(s.base_url, s.key_id, s.private_key_path, s.max_rps, s.page_size)
GRID = (10, 20, 30, 45, 60, 90, 120, 180, 240, 360, 480, 600)
N_UPDATES, K, STEPS, SIGMA_FLOOR = 8, 3, 8, 0.005
ZS = (1.0, 1.5, 2.0)
SAMPLE = 30.0
CRYPTO = ("KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M", "KXDOGE15M")
FX = ("KXEURUSD15M", "KXGBPUSD15M", "KXUSDJPY15M", "KXAUDUSD15M", "KXUSDCAD15M")
COMMODITY = ("KXGOLD15M", "KXSILVER15M", "KXWTI15M", "KXCOPPER15M", "KXNATGAS15M", "KXPLATINUM15M", "KXPALLADIUM15M")
MATCH = (("CS2 and Dota 2", ("KXCS2GAME", "KXDOTA2GAME")), ("eSoccer", ("KXESOCCERGAME",)), ("eBasketball", ("KXEBASKETBALLGAME",)))
MONTHS = {m: i + 1 for i, m in enumerate(["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}
NY = ZoneInfo("America/New_York")


def code_time(tk):
    m = re.match(r"[A-Z0-9]+-(\d\d)([A-Z]{3})(\d\d)(\d\d)(\d\d)", tk)
    return datetime(2000 + int(m[1]), MONTHS[m[2]], int(m[3]), int(m[4]), int(m[5]), tzinfo=NY).timestamp() if m else 0.0


def theta_at(ts, t):
    for w in GRID:
        if bisect.bisect_right(ts, t) - bisect.bisect_right(ts, t - w) >= N_UPDATES:
            return float(w)
    return None


def mid_at(ts, mids, t):
    i = bisect.bisect_right(ts, t) - 1
    return mids[i] if i >= 0 else None


def trend_z(ts, mids, t, theta):
    if ts[0] > t - (K + 1) * theta:
        return None
    m = [mid_at(ts, mids, t - i * theta) for i in range(STEPS + 1)]
    d = [m[i] - m[i + 1] for i in range(STEPS) if m[i] is not None and m[i + 1] is not None]
    if len(d) < 5:
        return None
    sigma = max(SIGMA_FLOOR, math.sqrt(sum(x * x for x in d) / len(d)))
    return (m[0] - m[K]) / (sigma * math.sqrt(K))



GIVEBACK, DROP_FAST, CONFIRM, HOLD_S, STOP_CONFIRM, PROGRESS = 0.90, 0.20, 10.0, 1.0, 10.0, 0.30


def simulate(ts, bid, ent, entry, trail, stop):
    """(bid sold at or None to hold, reason) from entry index `ent`."""
    peak, last, since = 0.0, bid[ent], ts[ent]
    below = stop_since = None
    for j in range(ent + 1, len(ts)):
        t, b = ts[j], bid[j]
        if b != last:
            if t - since >= HOLD_S:
                peak = max(peak, last)
            since = t
        elif t - since >= HOLD_S:
            peak = max(peak, b)
        last = b
        if t > ts[-1] - 10:
            break
        if stop is not None and 0 < b <= entry * (1 - stop):
            stop_since = stop_since or t
            if t - stop_since >= STOP_CONFIRM:
                return b, "stop"
        else:
            stop_since = None
        if not trail:
            continue
        if not (peak >= entry + PROGRESS * (1 - entry) - 1e-9 and peak > entry) or b < entry - 1e-9:
            below = None
            continue
        drop = max(0.01, GIVEBACK * (peak - entry))
        if b <= peak - max(DROP_FAST, drop) + 1e-9:
            return b, "trail"
        if b <= peak - drop + 1e-9:
            below = below or t
            if t - below >= CONFIRM:
                return b, "trail"
        else:
            below = None
    return None, None


EXITS = (("hold", False, None), ("trail", True, None), ("stop 30%", False, 0.30), ("stop 50%", False, 0.50), ("trail + stop 50%", True, 0.50))


def entry_rows(q, win_yes, cluster):
    out = []
    ts = [r[0] for r in q]
    for side in ("yes", "no"):
        bid = [r[1] if side == "yes" else 1 - r[2] for r in q]
        ask = [r[2] if side == "yes" else 1 - r[1] for r in q]
        mids = [(b + a) / 2 for b, a in zip(bid, ask, strict=True)]
        win = win_yes if side == "yes" else not win_yes
        t = ts[0] + 60
        while t < ts[-1] - 30:
            i = bisect.bisect_right(ts, t) - 1
            t += SAMPLE
            b, a = bid[i], ask[i]
            if not (0.70 <= a <= 0.90) or a - b > 0.08 or b <= 0:
                continue
            th = theta_at(ts, ts[i])
            if th is None:
                continue
            z = trend_z(ts, mids, ts[i], th)
            if z is None or z < 1.5:
                continue
            cost = a + fee(a)
            hold = ((1 - cost) / cost) if win else -1.0
            row = {"cl": cluster, "ask": a, "win": win, "t": ts[i]}
            for name, trail, stop in EXITS:
                if name == "hold":
                    row[name] = hold
                    continue
                sold, why = simulate(ts, bid, i, a, trail, stop)
                row[name] = hold if sold is None else (sold - fee(sold)) / cost - 1
                row[name + "#"] = sold is not None
            out.append(row)
            break  # one entry per market and side
    return out


def se(rows, key):
    per = defaultdict(list)
    for r in rows:
        per[r["cl"]].append(r[key])
    mm = [sum(v) / len(v) for v in per.values()]
    return math.sqrt(sum((x - sum(mm) / len(mm)) ** 2 for x in mm) / (len(mm) - 1) / len(mm)) * 100 if len(mm) > 1 else float("nan")


def run_15m(name, series):
    rows_db = main.db.execute(f"SELECT ticker, result FROM windows WHERE series IN ({','.join('?' * len(series))}) AND result IN ('yes','no')", tuple(series)).fetchall()
    rows = []
    for r in rows_db:
        q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND bid > 0 AND ask > 0 ORDER BY ts", (r["ticker"],)).fetchall()
        if len(q) >= 40:
            rows += entry_rows(q, r["result"] == "yes", r["ticker"].split("-")[1])
    print(f"\n{name}: {len(rows)} momentum entries (z >= 1.5, ask 70-90c), average price {statistics.mean(x['ask'] for x in rows):.2f}, won {statistics.mean(x['win'] for x in rows):.0%}")
    cut = sorted(x["t"] for x in rows)[int(0.6 * len(rows))]
    for ex, _, _ in EXITS:
        late = [x for x in rows if x["t"] >= cut]
        used = f", used on {statistics.mean(x[ex + '#'] for x in rows):.0%}" if ex + "#" in rows[0] else ""
        print(f"  {ex:18} {statistics.mean(x[ex] for x in rows):+.1%} ±{se(rows, ex):.1f}   later 40%: {statistics.mean(x[ex] for x in late):+.1%}{used}")


run_15m("crypto 15-minute", CRYPTO)
run_15m("FX 15-minute", FX)
run_15m("commodity 15-minute", COMMODITY)
