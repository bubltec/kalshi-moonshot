"""One momentum rule for every favorites market, with no clock in it: trend measured in the market's own activity time. Replayed from the tape. Places nothing.

theta     for a market at a moment: the shortest window W (10 s ... 10 min) in which the book updated at least 8 times. A busy market has a small theta, a quiet one a large theta;
          a market that never reaches 8 updates in 10 minutes is inactive and is skipped.
trend z   the mid's change over the last 3 thetas, divided by its typical change per theta times sqrt(3) (the typical change is the root-mean-square of the 8 previous theta-steps).
          z >= 1.5 means the price has moved up 1.5 'normal moves' over three activity-lengths.
buy       the first moment, per market and side, that the ask is 70-90c (book within 8c) and z clears a bar; hold to the result; fee at purchase only.
classes   crypto 15-minute, FX 15-minute, commodity 15-minute, and the match markets (CS2 + Dota 2, eSoccer, eBasketball). The same code for all.
Compared with the plain favorite (first 70-90c moment, any trend). Standard errors are taken across clusters: one 15-minute slot across coins, or one match.
    docker compose exec -T bot python - < scripts/theta_trend_study.py
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


def entries(q, win_yes, cluster):
    """One row per (side, rule): the first moment the rule is met. q = [(ts, yes bid, yes ask)]."""
    out = []
    ts = [r[0] for r in q]
    for side in ("yes", "no"):
        bid = [r[1] if side == "yes" else 1 - r[2] for r in q]
        ask = [r[2] if side == "yes" else 1 - r[1] for r in q]
        mids = [(b + a) / 2 for b, a in zip(bid, ask, strict=True)]
        win = win_yes if side == "yes" else not win_yes
        got = set()
        t = ts[0] + 60
        while t < ts[-1] - 30 and len(got) < 2 + len(ZS):
            i = bisect.bisect_right(ts, t) - 1
            t += SAMPLE
            b, a = bid[i], ask[i]
            if not (0.70 <= a <= 0.90) or a - b > 0.08 or b <= 0:
                continue
            th = theta_at(ts, ts[i])
            if th is None:
                continue
            z = trend_z(ts, mids, ts[i], th)
            if z is None:
                continue
            cost = a + fee(a)
            row = {"cl": cluster, "ask": a, "win": win, "ret": ((1 - cost) / cost) if win else -1.0, "theta": th, "z": z, "t": ts[i]}
            if "plain" not in got:
                got.add("plain"); out.append(("plain", row))
            if z <= 0 and "down" not in got:
                got.add("down"); out.append(("down", row))
            for zt in ZS:
                if z >= zt and zt not in got:
                    got.add(zt); out.append((zt, row))
    return out


def se(rows):
    per = defaultdict(list)
    for r in rows:
        per[r["cl"]].append(r["ret"])
    mm = [sum(v) / len(v) for v in per.values()]
    return math.sqrt(sum((x - sum(mm) / len(mm)) ** 2 for x in mm) / (len(mm) - 1) / len(mm)) * 100 if len(mm) > 1 else float("nan")


def report(name, n_markets, rules):
    print(f"\n{'=' * 118}\n{name}: {n_markets} finished markets replayed")
    allrows = [r for v in rules.values() for r in v]
    cut = sorted(r["t"] for r in rules.get("plain", []))
    split = cut[int(0.6 * len(cut))] if cut else 0
    for key, label in (("plain", "the plain favorite: first 70-90c moment"), ("down", "70-90c but not trending up (z <= 0)"), *((zt, f"trending up, z >= {zt}") for zt in ZS)):
        g = rules.get(key, [])
        if len(g) < 8:
            print(f"  {label:42} only {len(g)}")
            continue
        late = [r for r in g if r["t"] >= split]
        print(f"  {label:42} n={len(g):4} ({len({r['cl'] for r in g}):4} clusters) theta {statistics.median(r['theta'] for r in g):4.0f}s  price {statistics.mean(r['ask'] for r in g):.2f}  won {statistics.mean(r['win'] for r in g):.0%}  "
              f"hold {statistics.mean(r['ret'] for r in g):+.1%} ±{se(g):.1f}   later 40%: {statistics.mean(r['ret'] for r in late) if late else float('nan'):+.1%} (n={len(late)})")


def run_15m(name, series):
    rows = main.db.execute(f"SELECT ticker, result FROM windows WHERE series IN ({','.join('?' * len(series))}) AND result IN ('yes','no')", tuple(series)).fetchall()
    rules, n = defaultdict(list), 0
    for r in rows:
        q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND bid > 0 AND ask > 0 ORDER BY ts", (r["ticker"],)).fetchall()
        if len(q) < 40:
            continue
        n += 1
        for key, row in entries(q, r["result"] == "yes", r["ticker"].split("-")[1]):
            rules[key].append(row)
    report(name, n, rules)


run_15m("crypto 15-minute", CRYPTO)
run_15m("FX 15-minute", FX)
run_15m("commodity 15-minute", COMMODITY)
for name, prefixes in MATCH:
    rules, n = defaultdict(list), 0
    for prefix in prefixes:
        for (tk,) in tape.execute("SELECT ticker FROM quotes WHERE ticker LIKE ? GROUP BY ticker HAVING MAX(ts) < ? AND COUNT(*) >= 40", (prefix + "-%", time.time() - 1800)).fetchall():
            try:
                res = client.market(tk).get("result")
            except Exception:
                continue
            time.sleep(0.3)  # the request budget is shared with the live bot
            if res not in ("yes", "no"):
                continue
            q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND bid > 0 AND ask > 0 ORDER BY ts", (tk,)).fetchall()
            if len(q) < 40:
                continue
            n += 1
            for key, row in entries(q, res == "yes", tk.rsplit("-", 1)[0]):
                rules[key].append(row)
    report(name, n, rules)
