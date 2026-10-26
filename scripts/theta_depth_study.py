"""The activity-time momentum study again, but a buy counts only if the book offers enough size at the ask. Replayed from the tape. Places nothing.

Same rule as theta_trend_study.py. The new part is depth: for a YES buy the size is what is offered at the yes ask; for a NO buy it is the size bidding for YES (what a NO buyer takes). A buy needs
at least D contracts there, D = 0 meaning only that the size is recorded (the tape has sizes from 22 Oct 11:49 ET), 2 meaning about one $2 stake at these prices, 5 a stricter bar.
D = 0 is the like-for-like baseline: the same period, the same moments, no minimum. The share of 70-90c moments that had each depth is printed too.

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


DEPTHS = (0, 2, 5)
DEPTH_SEEN = defaultdict(lambda: defaultdict(int))  # class -> bucket -> moments in 70-90c


def entries(q, win_yes, cluster, klass=""):
    """One row per (depth, rule, side): the first moment the rule and the depth are met. q = [(ts, yes bid, yes ask, yes bid size, yes ask size)]."""
    out = []
    ts = [r[0] for r in q]
    for side in ("yes", "no"):
        bid = [r[1] if side == "yes" else 1 - r[2] for r in q]
        ask = [r[2] if side == "yes" else 1 - r[1] for r in q]
        size = [r[4] if side == "yes" else r[3] for r in q]  # what a buyer of this side takes
        mids = [(b + a) / 2 for b, a in zip(bid, ask, strict=True)]
        win = win_yes if side == "yes" else not win_yes
        got = set()
        t = ts[0] + 60
        while t < ts[-1] - 30:
            i = bisect.bisect_right(ts, t) - 1
            t += SAMPLE
            b, a = bid[i], ask[i]
            if not (0.70 <= a <= 0.90) or a - b > 0.08 or b <= 0 or size[i] is None:
                continue
            th = theta_at(ts, ts[i])
            if th is None:
                continue
            z = trend_z(ts, mids, ts[i], th)
            if z is None:
                continue
            sz = float(size[i])
            DEPTH_SEEN[klass]["moments"] += 1
            for d in (2, 5, 10):
                DEPTH_SEEN[klass][d] += 1 if sz >= d else 0
            cost = a + fee(a)
            row = {"cl": cluster, "ask": a, "win": win, "ret": ((1 - cost) / cost) if win else -1.0, "theta": th, "z": z, "t": ts[i], "size": sz}
            for d in DEPTHS:
                if sz < d:
                    continue
                if (d, "plain") not in got:
                    got.add((d, "plain")); out.append(((d, "plain"), row))
                for zt in ZS:
                    if z >= zt and (d, zt) not in got:
                        got.add((d, zt)); out.append(((d, zt), row))
    return out


def se(rows):
    per = defaultdict(list)
    for r in rows:
        per[r["cl"]].append(r["ret"])
    mm = [sum(v) / len(v) for v in per.values()]
    return math.sqrt(sum((x - sum(mm) / len(mm)) ** 2 for x in mm) / (len(mm) - 1) / len(mm)) * 100 if len(mm) > 1 else float("nan")


def report(name, n_markets, rules):
    seen = DEPTH_SEEN[name]
    m = max(1, seen["moments"])
    print(f"\n{'=' * 120}\n{name}: {n_markets} finished markets replayed; of {seen['moments']:,} sampled moments with the ask at 70-90c: "
          f"{seen[2] / m:.0%} had 2+ contracts offered, {seen[5] / m:.0%} had 5+, {seen[10] / m:.0%} had 10+")
    for d in DEPTHS:
        for key, label in (("plain", "the plain favorite"), (1.5, "trending up, z >= 1.5"), (2.0, "trending up, z >= 2.0")):
            g = rules.get((d, key), [])
            tag = f"D>={d}" if d else "sizes known"
            if len(g) < 8:
                print(f"  {tag:12} {label:26} only {len(g)}")
                continue
            print(f"  {tag:12} {label:26} n={len(g):4} ({len({r['cl'] for r in g}):4} clusters)  price {statistics.mean(r['ask'] for r in g):.2f}  won {statistics.mean(r['win'] for r in g):.0%}  "
                  f"hold {statistics.mean(r['ret'] for r in g):+.1%} ±{se(g):.1f}  median size {statistics.median(r['size'] for r in g):.0f}")


def run_15m(name, series):
    rows = main.db.execute(f"SELECT ticker, result FROM windows WHERE series IN ({','.join('?' * len(series))}) AND result IN ('yes','no')", tuple(series)).fetchall()
    rules, n = defaultdict(list), 0
    for r in rows:
        q = tape.execute("SELECT ts, bid, ask, bid_size, ask_size FROM quotes WHERE ticker=? AND bid > 0 AND ask > 0 ORDER BY ts", (r["ticker"],)).fetchall()
        if len(q) < 40:
            continue
        n += 1
        for key, row in entries(q, r["result"] == "yes", r["ticker"].split("-")[1], name):
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
            q = tape.execute("SELECT ts, bid, ask, bid_size, ask_size FROM quotes WHERE ticker=? AND bid > 0 AND ask > 0 ORDER BY ts", (tk,)).fetchall()
            if len(q) < 40:
                continue
            n += 1
            for key, row in entries(q, res == "yes", tk.rsplit("-", 1)[0], name):
                rules[key].append(row)
    report(name, n, rules)
