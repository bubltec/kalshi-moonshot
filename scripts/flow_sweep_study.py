"""Sweep the levers of the flow entry study, choosing on the earlier 60% of windows and scoring on the later 40%. Replays recorded ticks; places nothing.

For every 15-minute window with book sizes and trade flow recorded, an entry is sampled every 20 s (minutes 2-9.3) on each side, and replayed under every
(take-profit, max hold) pair: take-profit +5/+8/+12/+15c, hold 60/180/300 s (sell at the bid the first time it is `tp` over the price paid, else at the first quote after the hold).
Two ways in:
  market  buy at the ask (what flow_entry_study.py does)
  limit   post at the bid and wait up to 30 s; filled only when the ask comes down to our price (conservative: it needs the spread to close).
          ROI is per filled trade; the fill rate is shown. Fees are charged both ways as for a market order.
Filters (cutoffs from the train windows only): none, top third of book imbalance, top third of 60 s activity, both.
Cells: variant x price band (15-85c, 20-45c) x entry window (minutes 2-6, 6-9.3) x filter. In each cell the best (tp, hold) on train is scored on test; standard
errors are taken across windows because samples inside one window are not independent.
    docker compose exec -T bot python - < scripts/flow_sweep_study.py
"""
import bisect
import math
from collections import defaultdict
from datetime import UTC, datetime, timedelta

from kalshi_moonshot import config, db
from kalshi_moonshot.strategy import fee_per_contract as fee

s = config.reload()
tape = db.connect(s.tape_path, "tape", shared=True)
start_ts = tape.execute("SELECT MIN(ts) FROM quotes WHERE bid_size IS NOT NULL").fetchone()[0] + 600
SER = ("KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M", "KXDOGE15M", "KXGOLD15M", "KXSILVER15M", "KXWTI15M", "KXNATGAS15M", "KXCOPPER15M",
       "KXPLATINUM15M", "KXPALLADIUM15M", "KXEURUSD15M", "KXGBPUSD15M", "KXUSDJPY15M", "KXAUDUSD15M", "KXUSDCAD15M")
MONTHS = {m: i + 1 for i, m in enumerate(["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}
TPS = (0.05, 0.08, 0.12, 0.15)
HOLDS = (60, 180, 300)
COMBOS = [(tp, h) for tp in TPS for h in HOLDS]
LIMIT_WAIT = 30
OFFSETS = range(120, 561, 20)  # seconds into the window


def close_of(ticker):
    code = ticker.split("-")[1]
    y, mon, d, hh, mm = 2000 + int(code[:2]), MONTHS[code[2:5]], int(code[5:7]), int(code[7:9]), int(code[9:11])
    return (datetime(y, mon, d, hh, mm, tzinfo=UTC) + timedelta(hours=4)).timestamp()  # New York (EDT, UTC-4 until 1 Nov)


last_ts = tape.execute("SELECT MAX(ts) FROM quotes").fetchone()[0]
tickers = [r[0] for r in tape.execute("SELECT DISTINCT ticker FROM quotes WHERE ts >= ?", (start_ts,)).fetchall() if r[0].split("-")[0] in SER]
tickers = [t for t in tickers if close_of(t) - 900 >= start_ts and close_of(t) < last_ts - 120]
print(f"{len(tickers)} windows with book sizes and trade flow recorded for their whole length")


def scan(ts, bs, i0, t0, entry):
    """ROI for every (tp, hold) combo when we hold from `entry` at time t0 (i0 = last quote at or before it). None if the window ends first."""
    n = len(ts)
    hit = dict.fromkeys(TPS)
    hold = dict.fromkeys(HOLDS)
    j = i0 + 1
    while j < n:
        tj, b = ts[j], bs[j]
        for tp in TPS:
            if hit[tp] is None and b >= entry + tp:
                hit[tp] = j
        for h in HOLDS:
            if hold[h] is None and tj - t0 >= h:
                hold[h] = j
        if hold[HOLDS[-1]] is not None:
            break
        j += 1
    if hold[HOLDS[-1]] is None:
        return None
    cost = entry + fee(entry)
    out = []
    for tp in TPS:
        for h in HOLDS:
            hj, hd = hit[tp], hold[h]
            sold = bs[hj] if hj is not None and hj <= hd else bs[hd]
            out.append((sold - fee(sold)) / cost - 1)
    return tuple(out)


rows = []  # (variant, window, side, close, offset, ask, spread, imbalance, activity, rois or None)
for tk in tickers:
    close = close_of(tk)
    q = tape.execute("SELECT ts,bid,ask,bid_size,ask_size FROM quotes WHERE ticker=? AND ts>=? AND ts<? AND bid IS NOT NULL AND ask IS NOT NULL ORDER BY ts", (tk, close - 960, close)).fetchall()
    if len(q) < 60:
        continue
    fl = tape.execute("SELECT ts,yes_vol,no_vol FROM flow_1s WHERE ticker=? AND ts>=? AND ts<? ORDER BY ts", (tk, int(close - 1100), int(close))).fetchall()
    fts = [r[0] for r in fl]
    qts = [r[0] for r in q]
    n = len(q)
    for side in ("yes", "no"):
        bs = [r[1] if side == "yes" else 1 - r[2] for r in q]
        ak = [r[2] if side == "yes" else 1 - r[1] for r in q]
        for off in OFFSETS:
            t = close - 900 + off
            i = bisect.bisect_right(qts, t) - 1
            if i < 0 or q[i][3] is None or q[i][4] is None:
                continue
            bid, ask = bs[i], ak[i]
            if not (0 < ask < 1) or bid <= 0:
                continue
            sb, sa = (q[i][3], q[i][4]) if side == "yes" else (q[i][4], q[i][3])
            imb = (sb - sa) / (sb + sa) if (sb + sa) > 0 else None
            lo, hi = bisect.bisect_left(fts, t - 60), bisect.bisect_right(fts, t)
            act = sum(r[1] + r[2] for r in fl[lo:hi])
            key = (tk, side)
            r = scan(qts, bs, i, t, ask)
            if r is not None:
                rows.append(("market", key, close, off, ask, ask - bid, imb, act, r))
            jf = None
            j = i + 1
            while j < n and qts[j] - t <= LIMIT_WAIT:
                if ak[j] <= bid + 1e-9:
                    jf = j
                    break
                j += 1
            if jf is None:
                rows.append(("limit", key, close, off, ask, ask - bid, imb, act, None))
            else:
                r2 = scan(qts, bs, jf, qts[jf], bid)
                if r2 is not None:
                    rows.append(("limit", key, close, off, ask, ask - bid, imb, act, r2))
print(f"{len(rows)} sampled entries (market and limit)\n")

order = sorted({r[1][0] for r in rows}, key=close_of)
train_w = set(order[: int(0.6 * len(order))])
print(f"{len(order)} windows: first {len(train_w)} (to {datetime.fromtimestamp(close_of(order[len(train_w) - 1]), UTC):%d %b %H:%M} UTC) choose, the later {len(order) - len(train_w)} score\n")

BANDS = (("15-85c", 0.15, 0.85), ("20-45c", 0.20, 0.45))
WINDOWS = (("min 2-6", 120, 359), ("min 6-9.3", 360, 560))


def tercile_cut(vals):
    vals = sorted(vals)
    return vals[2 * len(vals) // 3] if len(vals) >= 90 else None


def mean_se(sample, c):
    """Mean ROI of combo c and its standard error across windows (side-windows), for rows that were traded."""
    per = defaultdict(list)
    for r in sample:
        per[r[1]].append(r[8][c])
    means = [sum(v) / len(v) for v in per.values()]
    if len(means) < 2:
        return None
    allv = [r[8][c] for r in sample]
    m = sum(allv) / len(allv)
    mm = sum(means) / len(means)
    se = math.sqrt(sum((x - mm) ** 2 for x in means) / (len(means) - 1) / len(means))
    return m, se


for band, lo, hi in BANDS:
    mk = [r for r in rows if r[0] == "market" and lo <= r[4] <= hi]
    tr = [r for r in mk if r[1][0] in train_w]
    spread = sum(r[5] for r in tr) / len(tr)
    imb_cut = tercile_cut([r[6] for r in tr if r[6] is not None])
    act_cut = tercile_cut([r[7] for r in tr])
    print("=" * 112)
    print(f"BAND {band}: avg spread {spread * 100:.1f}c, top-third cutoffs from train: imbalance >= {imb_cut:+.2f}, activity >= {act_cut:.0f}")
    filters = {
        "no filter": lambda r: True,
        "imbalance high": lambda r, c=imb_cut: r[6] is not None and r[6] >= c,
        "activity high": lambda r, c=act_cut: r[7] >= c,
        "both high": lambda r, a=imb_cut, b=act_cut: r[6] is not None and r[6] >= a and r[7] >= b,
    }
    for variant in ("market", "limit"):
        allv = [r for r in rows if r[0] == variant and lo <= r[4] <= hi]
        if variant == "limit":
            fr = sum(1 for r in allv if r[8] is not None) / len(allv)
            print(f"\n--- {variant} entries (post at the bid, wait {LIMIT_WAIT}s; filled {fr:.0%} of the time) ---")
        else:
            print(f"\n--- {variant} entries (buy at the ask) ---")
        print(f"{'window':10}{'filter':16}{'best on train (tp, hold)':>26}{'train ROI':>11}{'TEST ROI':>10}{'± se':>7}{'n test':>8}{'plain 8c/180s on test':>24}{'combos > 0 on test':>20}")
        for wname, w0, w1 in WINDOWS:
            for fname, f in filters.items():
                cell = [r for r in allv if r[8] is not None and w0 <= r[3] <= w1 and f(r)]
                train = [r for r in cell if r[1][0] in train_w]
                test = [r for r in cell if r[1][0] not in train_w]
                if len(train) < 200 or len(test) < 100:
                    print(f"{wname:10}{fname:16}  too few entries (train {len(train)}, test {len(test)})")
                    continue
                best = max(range(len(COMBOS)), key=lambda c: sum(r[8][c] for r in train) / len(train))
                trm = sum(r[8][best] for r in train) / len(train)
                te = mean_se(test, best)
                plain = sum(r[8][COMBOS.index((0.08, 180))] for r in test) / len(test)
                pos = sum(1 for c in range(len(COMBOS)) if sum(r[8][c] for r in test) / len(test) > 0)
                tp, h = COMBOS[best]
                print(f"{wname:10}{fname:16}{f'+{tp * 100:.0f}c, {h}s':>26}{trm:>+11.1%}{te[0]:>+10.1%}{('%.1f%%' % (te[1] * 100)):>7}{len(test):>8}{plain:>+24.1%}{pos:>17}/{len(COMBOS)}")
    print()
