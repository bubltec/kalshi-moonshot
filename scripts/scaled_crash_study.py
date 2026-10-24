"""Crash exit with a drop scaled to each series' own 10-second bid noise, against the fixed 4c/12c rule and holding.
Entry: first quote at minutes 4-10.5 with the side's ask 55-85c and spread <= 3c (any 15-minute series); exit rules only.
Run:  docker compose exec -T bot python - < scripts/scaled_crash_study.py
"""
import bisect
import math
import time
from collections import defaultdict

from kalshi_moonshot import config
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee
from kalshi_moonshot.tape import Tape

s = config.reload()
st = Store(s.db_path, s.exclude_versions)
tape = Tape(s.tape_path).db
SLIP = 0.02
GROUP = {"KXPALLADIUM15M": "metals", "KXPLATINUM15M": "metals", "KXCOPPER15M": "metals", "KXGOLD15M": "metals", "KXSILVER15M": "metals",
         "KXWTI15M": "energy", "KXBRENT15M": "energy", "KXNATGAS15M": "energy"}
wins = st.db.execute("select ticker, series, close_ts, result from windows where result in ('yes','no') and series like '%15M'").fetchall()
cut = time.time() - 4 * 86400


def noise(rows):
    ts = [r[0] for r in rows]
    out = []
    for i in range(0, len(rows), 5):
        j = bisect.bisect_right(ts, ts[i] + 10)
        if j - i >= 2:
            seg = [r[1] for r in rows[i:j]]
            out.append(max(seg) - min(seg))
    out.sort()
    return out[int(len(out) * 0.75)] if len(out) > 200 else None


quotes, q75 = {}, {}
per_series = defaultdict(list)
for w in wins:
    q = tape.execute("select ts,bid,ask from quotes where ticker=? and bid is not null and ask is not null and ts between ? and ? order by ts", (w["ticker"], float(w["close_ts"]) - 900, float(w["close_ts"]))).fetchall()
    if len(q) >= 10:
        quotes[w["ticker"]] = q
        per_series[w["series"]] += [(r[0], r[1]) for r in q if 0.4 <= r[1] <= 0.95 and r[2] - r[1] <= 0.04]
for ser, rows in per_series.items():
    rows.sort()
    n = noise(rows)
    if n:
        q75[ser] = n


def run(path, cost, drop, fast, act=0.10, hold=3.0, confirm=10.0):
    peak, peak_since, armed_bid, below = 0.0, None, None, None
    last = None
    for ts, bid in path:
        if bid != last:
            last, since = bid, ts
        if bid > peak and (bid - SLIP - fee(bid)) > cost * (1 + act):
            if ts - since >= hold:
                peak = bid
        if peak:
            if peak - bid >= fast:
                return bid
            if peak - bid >= drop:
                below = below if below is not None else ts
                if ts - below >= confirm:
                    return bid
            else:
                below = None
    return None


def roi(cost, bid, won):
    ret = (bid - SLIP - fee(bid)) if bid is not None else (1.0 if won else 0.0)
    return ret / cost - 1


variants = {"hold": None, "fixed 4c / 12c": ("fixed", 0.04, 0.12), "1.0 x noise": ("k", 1.0, 3.0), "1.5 x noise": ("k", 1.5, 3.0), "2.0 x noise": ("k", 2.0, 3.0), "3.0 x noise": ("k", 3.0, 4.0)}
res = defaultdict(lambda: defaultdict(list))
for w in wins:
    q = quotes.get(w["ticker"])
    ser = w["series"]
    if q is None or ser not in q75:
        continue
    close = float(w["close_ts"])
    e = None
    for i, (ts, b, a) in enumerate(q):
        if 4 <= (ts - (close - 900)) / 60 <= 10.5:
            for side in ("yes", "no"):
                ask, bid = (a, b) if side == "yes" else (1 - b, 1 - a)
                if 0.55 <= ask <= 0.85 and ask - bid <= 0.03:
                    e = (i, side, ask)
                    break
        if e:
            break
    if not e:
        continue
    i0, side, ask = e
    cost, won = ask + fee(ask), w["result"] == side
    path = [(ts, b if side == "yes" else 1 - a) for ts, b, a in q[i0 + 1:]]
    g = GROUP.get(ser, "currencies" if ser.startswith(("KXEUR", "KXGBP", "KXUSD", "KXAUD")) else "crypto")
    for name, v in variants.items():
        if v is None:
            exit_bid = None
        elif v[0] == "fixed":
            exit_bid = run(path, cost, v[1], v[2])
        else:
            exit_bid = run(path, cost, max(0.03, v[1] * q75[ser]), max(0.08, v[2] * q75[ser]))
        res[g][name].append(roi(cost, exit_bid, won))
        res["ALL"][name].append(res[g][name][-1])


def ms(v):
    m = sum(v) / len(v)
    return f"{m:+6.1%} ±{math.sqrt(sum((x - m) ** 2 for x in v) / (len(v) - 1) / len(v)):.1%}"


print("75th-percentile 10 s bid range per series (the noise unit):", {k.replace('KX', '').replace('15M', ''): round(v, 2) for k, v in sorted(q75.items(), key=lambda kv: kv[1])})
for g in ("metals", "energy", "currencies", "crypto", "ALL"):
    n = len(res[g]["hold"])
    print(f"\n{g}  ({n} bets)")
    for name in variants:
        print(f"  {name:16} {ms(res[g][name])}")
