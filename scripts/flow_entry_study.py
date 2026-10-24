"""Does order flow at the moment of entry predict a bounce? Replays recorded ticks; places nothing.

For every 15-minute window the tape saw while it was recording book sizes and trade flow, an entry is sampled every 20 s (minutes 2-11) on each side:
  book_imbalance   (size bidding for the side - size offered against it) / their sum, from the latest quote
  flow_60          taker volume buying the side minus the other side, over the prior 60 s, as a share of the total (None if too quiet)
  flow_15          the same over the prior 15 s
  activity_60      contracts traded in the prior 60 s
Outcome: buy at the ask, sell the first time the bid is +8c, else after 3 minutes at the bid; fees both ways. "bounce" = the +8c was reached.
Terciles are compared with the standard error taken across windows (samples inside one window are not independent).
    docker compose exec -T bot python - < scripts/flow_entry_study.py
"""
import bisect
import math
import random
from collections import defaultdict
from datetime import UTC, datetime, timedelta

from kalshi_moonshot import config, db, trips
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee

s = config.reload()
st = Store(s.db_path, s.exclude_versions)
tape = db.connect(s.tape_path, "tape", shared=True)
start_ts = tape.execute("SELECT MIN(ts) FROM quotes WHERE bid_size IS NOT NULL").fetchone()[0] + 600
SER = ("KXGOLD15M", "KXSILVER15M", "KXWTI15M", "KXNATGAS15M", "KXCOPPER15M", "KXPLATINUM15M", "KXPALLADIUM15M", "KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M", "KXDOGE15M",
       "KXEURUSD15M", "KXGBPUSD15M", "KXUSDJPY15M", "KXAUDUSD15M", "KXUSDCAD15M")
MONTHS = {m: i + 1 for i, m in enumerate(["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}


def close_of(ticker):
    """KXBTC15M-26OCT221845-45: the 18:45 New York close on 22 Oct 2026 (EDT = UTC-4 until 1 Nov)."""
    code = ticker.split("-")[1]
    y, mon, d, hh, mm = 2000 + int(code[:2]), MONTHS[code[2:5]], int(code[5:7]), int(code[7:9]), int(code[9:11])
    return (datetime(y, mon, d, hh, mm, tzinfo=UTC) + timedelta(hours=4)).timestamp()


tickers = [r[0] for r in tape.execute("SELECT DISTINCT ticker FROM quotes WHERE ts >= ?", (start_ts,)).fetchall() if r[0].split("-")[0] in SER]
tickers = [t for t in tickers if close_of(t) - 900 >= start_ts and close_of(t) < max(r[0] for r in tape.execute("SELECT MAX(ts) FROM quotes").fetchall()) - 120]
print(f"{len(tickers)} windows with book sizes and trade flow recorded for their whole length")
random.seed(5)
samples = []  # (window id, side, ask, features dict, roi, bounced)


def run_ticker(tk, side, t_list=None, only=None):
    close = close_of(tk)
    q = tape.execute("SELECT ts,bid,ask,bid_size,ask_size FROM quotes WHERE ticker=? AND ts>=? AND ts<? AND bid IS NOT NULL AND ask IS NOT NULL ORDER BY ts", (tk, close - 960, close)).fetchall()
    if len(q) < 60:
        return []
    fl = tape.execute("SELECT ts,yes_vol,no_vol FROM flow_1s WHERE ticker=? AND ts>=? AND ts<? ORDER BY ts", (tk, int(close - 1100), int(close))).fetchall()
    fts = [r[0] for r in fl]
    qts = [r[0] for r in q]
    out = []
    times = t_list if t_list is not None else [close - 900 + 60 * 2 + 20 * k for k in range(0, 28)]
    for t in times:
        i = bisect.bisect_right(qts, t) - 1
        if i < 0 or q[i][3] is None or q[i][4] is None:
            continue
        _, yb, ya, ybs, yas = q[i]
        bid, ask = (yb, ya) if side == "yes" else (1 - ya, 1 - yb)
        sb, sa = (ybs, yas) if side == "yes" else (yas, ybs)  # size bidding for our side / offered against it (NO: swap)
        if ask <= 0 or ask >= 1:
            continue
        def flow(sec):
            lo, hi = bisect.bisect_left(fts, t - sec), bisect.bisect_right(fts, t)
            mine = sum(r[1] if side == "yes" else r[2] for r in fl[lo:hi])
            other = sum(r[2] if side == "yes" else r[1] for r in fl[lo:hi])
            tot = mine + other
            return ((mine - other) / tot if tot >= 3 else None), tot
        f60, act = flow(60)
        f15, _ = flow(15)
        feats = {"book_imbalance": (sb - sa) / (sb + sa) if (sb + sa) > 0 else None, "flow_60": f60, "flow_15": f15, "activity_60": act}
        # outcome
        sold, bounced = None, False
        for j in range(i + 1, len(q)):
            tj = q[j][0]
            bj = q[j][1] if side == "yes" else 1 - q[j][2]
            if bj >= ask + 0.08:
                sold, bounced = bj, True
                break
            if tj - t >= 180:
                sold = bj
                break
        if sold is None:
            continue
        roi = (sold - fee(sold)) / (ask + fee(ask)) - 1
        out.append((ask, feats, roi, bounced))
    return out


for tk in tickers:
    for side in ("yes", "no"):
        for ask, feats, roi, bounced in run_ticker(tk, side):
            samples.append((tk, side, ask, feats, roi, bounced))
print(f"{len(samples)} sampled entries\n")


def table(rows, feature, title):
    vals = sorted(r[3][feature] for r in rows if r[3][feature] is not None)
    if len(vals) < 300:
        print(f"{title}: too few samples ({len(vals)})")
        return
    lo, hi = vals[len(vals) // 3], vals[2 * len(vals) // 3]
    groups = defaultdict(list)
    for r in rows:
        v = r[3][feature]
        if v is None:
            continue
        groups["low" if v <= lo else "middle" if v <= hi else "high"].append(r)
    print(f"{title}  (terciles split at {lo:+.2f} and {hi:+.2f})")
    print(f"  {'':8}{'entries':>9}{'windows':>9}{'bounce +8c':>12}{'avg ROI':>10}{'(± across windows)':>20}")
    for name in ("low", "middle", "high"):
        g = groups[name]
        per = defaultdict(list)
        for r in g:
            per[(r[0], r[1])].append(r[4])
        means = [sum(v) / len(v) for v in per.values()]
        m = sum(means) / len(means)
        se = math.sqrt(sum((x - m) ** 2 for x in means) / (len(means) - 1) / len(means)) if len(means) > 1 else 0
        b = sum(1 for r in g if r[5]) / len(g)
        print(f"  {name:8}{len(g):9}{len(per):9}{b:12.1%}{sum(r[4] for r in g) / len(g):+10.1%}{('±%.1f%%' % (se * 100)):>20}")
    print()


for label, lo, hi in (("ALL prices 15-85c", 0.15, 0.85), ("CHEAP side 20-45c (the dip-buy range)", 0.20, 0.45)):
    rows = [r for r in samples if lo <= r[2] <= hi]
    print("=" * 100)
    print(f"{label}: {len(rows)} entries, overall bounce {sum(1 for r in rows if r[5]) / len(rows):.1%}, avg ROI {sum(r[4] for r in rows) / len(rows):+.1%}\n")
    table(rows, "book_imbalance", "book size imbalance (more size bidding for our side -> high)")
    table(rows, "flow_60", "trade flow, last 60 s (more taker buying of our side -> high)")
    table(rows, "flow_15", "trade flow, last 15 s")
    table(rows, "activity_60", "activity, contracts traded in the last 60 s")

# your own trades in the same period
tr = [t for t in trips.build_trips(st) if t.done and t.source == "manual" and t.series.endswith("15M") and t.opened_ts >= start_ts and t.series in SER]
print("=" * 100)
print(f"YOUR manual 15-minute trades since the recording began: {len(tr)}")
mine = []
for t in tr:
    res = run_ticker(t.ticker, t.side, t_list=[t.opened_ts])
    if res:
        ask, feats, roi, bounced = res[0]
        mine.append((t, feats, t.cashed_out, t.pnl))
print(f"  with flow data at entry: {len(mine)}; cashed out: {sum(1 for m in mine if m[2])}")
def avg(rows, key):
    v = [m[1][key] for m in rows if m[1][key] is not None]
    return (sum(v) / len(v), len(v)) if v else (0.0, 0)
base = [r[3] for r in samples if 0.15 <= r[2] <= 0.85]
for key in ("book_imbalance", "flow_60", "flow_15", "activity_60"):
    ob = [x[key] for x in base if x[key] is not None]
    ordinary = sum(ob) / len(ob)
    c, nc = avg([m for m in mine if m[2]], key)
    h, nh = avg([m for m in mine if not m[2]], key)
    a, na = avg(mine, key)
    print(f"  {key:16} ordinary {ordinary:+8.2f} | yours {a:+8.2f} (n={na}) | cash-outs {c:+8.2f} (n={nc}) | held to settlement {h:+8.2f} (n={nh})")
